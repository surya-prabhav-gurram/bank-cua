"""
Who is signed in, and what that identity is allowed to ask for.

The decision this file exists to contain
----------------------------------------
Everything downstream of here already refuses to take orders from the caller:
`config/service.yaml` decides whether a capability may perform its irreversible
step, `bankcua/safety/credentials.py` decides which secret an operator ALIAS
resolves to, and the replay engine decides what a capability is allowed to do to
a page. What none of them knew until now is WHO IS SITTING IN FRONT OF THE
CONSOLE -- so a console window opened by anyone who could reach the port and one
opened by a supervisor were the same caller.

This module supplies that missing subject, and it does so in a way that cannot be
asserted by the request:

  * a person signs in ONCE, against a principal store the deployment configures;
  * the console mints a signed, expiring session token that carries a USERNAME
    and nothing else -- no role, no operator alias, because a claim the token
    carried would be a claim an attacker only has to forge once;
  * every consumer (the portal, the dashboard, the capability API) re-resolves
    that username against the store, so a role change or a deletion takes effect
    on the next request rather than at the next sign-in;
  * the operator ALIAS a run executes as is derived from the principal, never
    read from the request body. A teller's session cannot name `super1`.

The one thing a token does carry: the BRANCH
--------------------------------------------
A branch is not an identity and cannot be re-derived from the principal store,
because it is a CHOICE the person makes at the door -- the same choice they would
make at MERIDIAN's own sign-on screen, which has a branch field. So it rides in
the token, and that is a deliberate exception to the paragraph above.

What keeps it honest is that the claim is not load-bearing. It grants nothing:
`config/service.yaml` lists the branches this deployment recognises, and
`bankcua/service.py` re-validates the token's branch against that list on every
invocation, refusing one it does not know rather than passing it through to the
host. So a forged branch buys an attacker a refusal, and an edited one buys them
another branch's NAME in their own audit trail -- never an action they could not
already perform.

If a branch is ever made to gate what an operator may reach, that stops being
true and the choice belongs in server-side session state instead. It is written
down here because that is exactly the kind of change that gets made without
noticing what it invalidates.

One kind of subject, and why there is no second
------------------------------------------------
Every principal here is STAFF: `supervisor` or `teller`, each mapping onto a
Meridian operator of the same name, acting as their own alias.

There is deliberately no member sign-in. MERIDIAN CORE is a back office, and its
users are the institution's own staff; a credit union's members reach their
accounts through online banking, a separate application with its own threat
model. Seating a member at the core banking console would be a category error,
and no amount of per-request scoping would make it the right place to put them.
The stronger claim is also the simpler one: a member cannot reach this console
at all, so nothing downstream has to be careful on their behalf.

A sign-in whose role is not a staff role is therefore REFUSED by the store
rather than admitted with a narrower action space -- see `PrincipalStore.get`.
Which capabilities each role may invoke is still declared per capability in
`config/service.yaml` (`allowed_principal_roles`), because a teller and a
supervisor are not the same subject either.

The seam: swap `PrincipalStore` for an IdP, LDAP or the institution's directory
without touching the portal, the dashboard, the chatbot or the API.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, replace

#: The roles this console recognises. There is no other kind of subject: a
#: sign-in that is not one of these is refused by `PrincipalStore.get` rather
#: than admitted with a narrower action space.
STAFF_ROLES = ("supervisor", "teller")

#: What a capability may be invoked by when `config/service.yaml` says nothing.
#: Stated rather than left implicit, so that a capability nobody has configured
#: cannot silently widen if a role is ever added to STAFF_ROLES.
DEFAULT_PRINCIPAL_ROLES = list(STAFF_ROLES)

SESSION_HEADER = "X-Bankcua-Session"
SESSION_COOKIE = "bankcua_session"
SESSION_SECRET_ENV = "BANKCUA_SESSION_SECRET"
DEFAULT_SECRET_PATH = "config/session.key"
DEFAULT_PRINCIPALS_PATH = "config/principals.json"

#: Eight hours: a working day. Long enough that a demo or a shift does not stop
#: for a re-login, short enough that a token copied out of a browser is not a
#: permanent credential.
DEFAULT_TTL_S = 8 * 3600


class AuthError(RuntimeError):
    """A principal could not be established.

    Deliberately not a "carry on unauthenticated" path. Every caller of this
    module is deciding whether to act on a member's account, and the safe answer
    to "I do not know who this is" is to stop. (The account is a member's; the
    person acting on it is always staff.)
    """


# ---------------------------------------------------------------------------
# Principals
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    """Who is signed in, and the only facts authorisation is allowed to consult.

    `acts_as` is the operator ALIAS whose Meridian identity executes this
    principal's work -- always their own, since every principal is staff. It
    stays an explicit field rather than an alias for `username` because a
    deployment may name its Meridian operators differently from its sign-ins.

    `branch` is where they signed in, which is session context rather than
    identity -- the same operator signs in at a different branch tomorrow. It
    lives on the Principal so that it reaches `principal.json` beside every
    run's evidence without a second thing to remember to thread through.
    """
    username: str
    role: str
    display_name: str = ""
    acts_as: str | None = None
    branch: str = ""

    def to_public(self) -> dict:
        """What may be shown in a UI. There is nothing else to withhold -- a
        Principal never holds a secret, which is what lets the portal hand it to
        the browser without a filtering step someone can forget.

        This is also what is written to `principal.json` next to a run, so the
        branch a task was performed from is recorded by the same act that
        records who performed it."""
        return {"username": self.username, "role": self.role,
                "display_name": self.display_name or self.username,
                "acts_as": self.acts_as, "branch": self.branch}


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
_PBKDF2_ROUNDS = 200_000


def hash_password(password: str, *, rounds: int = _PBKDF2_ROUNDS) -> str:
    """`pbkdf2_sha256$rounds$salt$hash`.

    A demo could compare plaintext and nothing on screen would look different.
    It is stored hashed because the principal file sits on the same disk as the
    evidence tree, and "it was only a demo password" is the sentence that
    precedes the reused one.
    """
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt), rounds).hex()
    return f"pbkdf2_sha256${rounds}${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check against an encoded hash, or against a plaintext
    entry.

    Plaintext is accepted because this deployment's principal file is edited by
    hand for the demo and a reader must be able to change a password without
    running a tool. It is compared in constant time as well, so the only thing
    it costs is at-rest exposure -- which `bankcua portal hash-passwords` closes
    in one command.
    """
    if not encoded:
        return False
    if not encoded.startswith("pbkdf2_sha256$"):
        return hmac.compare_digest(password, encoded)
    try:
        _algo, rounds, salt, digest = encoded.split("$", 3)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                        bytes.fromhex(salt), int(rounds)).hex()
    except Exception:
        return False
    return hmac.compare_digest(candidate, digest)


class PrincipalStore:
    """The people who may sign in, from a JSON file read fresh on every call.

    Read fresh rather than cached at import for the same reason the credential
    store is: revoking someone must take effect now, not at the next restart.
    Format (see `config/principals.example.json`):

        {"super1":  {"role": "supervisor", "acts_as": "super1",
                     "password_hash": "pbkdf2_sha256$..."},
         "teller1": {"role": "teller", "acts_as": "teller1",
                     "password_hash": "pbkdf2_sha256$..."}}
    """

    ENV_VAR = "BANKCUA_PRINCIPALS_FILE"

    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get(self.ENV_VAR, DEFAULT_PRINCIPALS_PATH)

    # ---- reading ---------------------------------------------------------
    def _load(self) -> dict:
        if not os.path.exists(self.path):
            raise AuthError(
                f"no principal store at {self.path!r}. Copy "
                f"config/principals.example.json to config/principals.json, or "
                f"run `python -m bankcua.cli portal init`.")
        with open(self.path) as fh:
            raw = json.load(fh)
        return {k: v for k, v in raw.items()
                if isinstance(v, dict) and not k.startswith("_")}

    def usernames(self) -> list[str]:
        try:
            return sorted(self._load())
        except AuthError:
            return []

    def get(self, username: str) -> Principal:
        """Resolve a username to a live principal, or refuse.

        This is the function that makes a session token safe to hold only a
        username: every request re-derives the role and the operator alias from
        the store, so a token minted before a demotion does not still carry the
        promotion.

        It is also the single choke point behind the "staff only" claim. A role
        this console does not recognise is REFUSED here rather than admitted
        with whatever the default action space happens to be -- so it does not
        matter what a principal file has been edited to say, an entry that is
        not a teller or a supervisor cannot become a session.
        """
        entry = self._load().get(username)
        if entry is None:
            raise AuthError(f"unknown sign-in {username!r}")
        role = str(entry.get("role") or "")
        if role not in STAFF_ROLES:
            raise AuthError(
                f"{username!r} has role {role or '(none)'!r}, which is not a "
                f"MERIDIAN operator role ({', '.join(STAFF_ROLES)}); this "
                f"console signs in the institution's own staff only")
        return Principal(username=username, role=role,
                         display_name=str(entry.get("display_name") or username),
                         acts_as=(str(entry["acts_as"])
                                  if entry.get("acts_as") else None))

    # ---- authenticating --------------------------------------------------
    def authenticate(self, username: str, password: str) -> Principal:
        """Verify a sign-in. Wrong user and wrong password are the same answer.

        Both cost roughly the same time, too: an unknown username still runs a
        PBKDF2 round against a decoy, so the response time does not tell an
        attacker which sign-ins exist. The operator names are few and guessable,
        which is exactly why the timing oracle is worth closing: the list is not
        secret, but which entries are LIVE should not be free to discover.
        """
        try:
            entry = self._load().get(username)
        except AuthError:
            raise
        encoded = (entry or {}).get("password_hash") or (entry or {}).get("password")
        if entry is None or not encoded:
            verify_password(password, hash_password("decoy"))
            raise AuthError("that sign-in and password do not match")
        if not verify_password(password, str(encoded)):
            raise AuthError("that sign-in and password do not match")
        return self.get(username)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def read_session_secret(path: str = DEFAULT_SECRET_PATH) -> bytes:
    """The signing key, from the environment or from a file.

    Two processes verify these tokens -- the console and the capability API --
    and they must agree without either being able to mint one the other would
    not accept. A shared key on disk (0600, gitignored by `*.key`) is the
    smallest thing that achieves that; the environment variable is what a real
    deployment uses instead.
    """
    env = os.environ.get(SESSION_SECRET_ENV)
    if env:
        return env.encode()
    if os.path.exists(path):
        with open(path, "rb") as fh:
            raw = fh.read().strip()
        if raw:
            return raw
    raise AuthError(
        f"no session key: set {SESSION_SECRET_ENV} or create {path!r} "
        f"(`python -m bankcua.cli portal init` writes one)")


def ensure_session_secret(path: str = DEFAULT_SECRET_PATH) -> bytes:
    """Create the signing key if it is absent. Only the console does this.

    The API and the dashboard READ this key and never write it: a verifier that
    can create a key it then trusts will happily verify tokens nobody minted.
    """
    env = os.environ.get(SESSION_SECRET_ENV)
    if env:
        return env.encode()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_hex(32).encode())
    return read_session_secret(path)


class SessionSigner:
    """Mint and verify session tokens: `payload.signature`, HMAC-SHA256.

    The payload carries a username, an expiry, and the branch chosen at the
    door. Every other fact -- the role, and which operator alias the work runs
    as -- is looked up server-side at use time. A token is therefore a statement
    about who signed in, never a statement about what they may do, which means
    forging a permission requires forging an identity that a store must also
    recognise.

    The branch is the one carried value, and it is carried because it cannot be
    looked up: it is a choice, not a property. It grants nothing on its own --
    see the module docstring for why that has to stay true.
    """

    def __init__(self, secret: bytes, ttl_s: float = DEFAULT_TTL_S):
        self.secret = secret
        self.ttl_s = ttl_s

    def mint(self, principal: Principal | str, *, branch: str = "",
             now: float | None = None) -> str:
        username = (principal.username if isinstance(principal, Principal)
                    else str(principal))
        if not branch and isinstance(principal, Principal):
            branch = principal.branch
        issued = time.time() if now is None else now
        claims = {"u": username, "iat": round(issued),
                  "exp": round(issued + self.ttl_s)}
        if branch:
            # Omitted entirely when empty rather than written as "", so a token
            # minted on the direct agent path is byte-identical to the ones this
            # system issued before branches existed -- an old session stays
            # valid, and falls back to the operator's configured branch.
            claims["b"] = str(branch)
        payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload)}"

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self.secret, payload.encode(),
                             hashlib.sha256).digest())

    def claims(self, token: str, *, now: float | None = None) -> dict:
        """Validate a token and return what it says, or raise.

        Signature first, then expiry, then the one field that must be present.
        Nothing here interprets the branch: this returns what the token SAYS,
        and deciding whether that branch is one this deployment recognises
        belongs to the service, which is the thing that holds the list.
        """
        if not token or "." not in token:
            raise AuthError("no session")
        payload, _, signature = token.partition(".")
        if not hmac.compare_digest(self._sign(payload), signature):
            raise AuthError("session signature does not verify")
        try:
            claims = json.loads(_unb64(payload))
        except Exception as ex:
            raise AuthError("malformed session") from ex
        if not isinstance(claims, dict):
            raise AuthError("malformed session")
        if float(claims.get("exp", 0)) < (time.time() if now is None else now):
            raise AuthError("session expired; sign in again")
        if not str(claims.get("u") or ""):
            raise AuthError("session names no principal")
        return claims

    def verify(self, token: str, *, now: float | None = None) -> str:
        """Return the username a valid, unexpired token names, or raise."""
        return str(self.claims(token, now=now)["u"])


class SessionAuthority:
    """Token -> live Principal. The one call every consumer makes.

    Bundles the signer and the store because using one without the other is the
    mistake worth designing out: verifying the signature and then trusting the
    token's own claims is exactly the shortcut this module exists to prevent.
    """

    def __init__(self, store: PrincipalStore | None = None,
                 signer: SessionSigner | None = None,
                 secret_path: str = DEFAULT_SECRET_PATH,
                 principals_path: str | None = None,
                 ttl_s: float = DEFAULT_TTL_S):
        self.store = store or PrincipalStore(principals_path)
        self._signer = signer
        self._secret_path = secret_path
        self._ttl_s = ttl_s

    @property
    def signer(self) -> SessionSigner:
        # Resolved lazily so a process that never sees a token (an offline CLI
        # replay, say) does not fail to start for want of a key it will not use.
        if self._signer is None:
            self._signer = SessionSigner(read_session_secret(self._secret_path),
                                         ttl_s=self._ttl_s)
        return self._signer

    def mint(self, principal: Principal, *, branch: str = "") -> str:
        return self.signer.mint(principal, branch=branch or principal.branch)

    def principal(self, token: str) -> Principal:
        """Token -> live Principal, with the branch it was signed in at.

        The username is re-resolved against the store, as always; the branch is
        taken from the token because there is nowhere else it could come from.
        Whether that branch is one the deployment recognises is decided by
        `bankcua/service.py`, which owns the list -- this is deliberately not
        the place, so that an unconfigured branch produces a refusal a caller
        can read rather than a session that silently fails to exist.
        """
        claims = self.signer.claims(token)
        principal = self.store.get(str(claims["u"]))
        branch = str(claims.get("b") or "")
        return replace(principal, branch=branch) if branch else principal

    def sign_in(self, username: str, password: str,
                branch: str = "") -> tuple[Principal, str]:
        principal = self.store.authenticate(username, password)
        if branch:
            principal = replace(principal, branch=branch)
        return principal, self.mint(principal)


def token_from_request(headers, cookies=None) -> str:
    """Pull a session token off a request, header first then cookie.

    The header is what one service sends another; the cookie is what a browser
    sends the console. Never a query parameter: a token in a URL is a token in
    an access log, a referrer, and a screenshot of the address bar.
    """
    token = ""
    if headers is not None:
        token = headers.get(SESSION_HEADER) or ""
        if not token:
            auth = headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
    if not token and cookies is not None:
        token = cookies.get(SESSION_COOKIE) or ""
    return token.strip()


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Denial:
    """A refusal, in the same shape the service and the engine already use, so a
    caller branches on one contract however far down the refusal came from."""
    code: str
    requirement: str
    reason: str

    def to_json(self) -> dict:
        return {"code": self.code, "requirement": self.requirement,
                "reason": self.reason}


def may_invoke(principal: Principal, allowed_roles) -> Denial | None:
    """Is this capability open to this principal's role at all?

    Deliberately about the SIGN-IN role and not about the operator alias. The
    two travel together today, since every principal acts as their own alias --
    but they are different questions, and `config/service.yaml` asks both
    (`allowed_principal_roles` here, `requires_role` against the credential
    store). Collapsing them would make the narrower of the two unenforceable.
    """
    roles = list(allowed_roles or DEFAULT_PRINCIPAL_ROLES)
    if principal.role in roles:
        return None
    return Denial(
        "CAPABILITY_NOT_PERMITTED_FOR_ROLE",
        f"a sign-in with one of these roles: {', '.join(sorted(roles))}",
        f"{principal.username!r} is signed in as {principal.role!r}, which "
        f"cannot use this capability")


def operator_alias_for(principal: Principal, default_operator: str = "") -> str:
    """The Meridian alias this principal's work executes as.

    Every principal is staff, so this is always their OWN identity: the alias
    their principal record names, or their username. Nothing here consults the
    request, which is what makes `OPERATOR_NOT_SESSION` in bankcua/service.py
    enforceable -- a teller's session resolves to `teller1` no matter what the
    body asks for.

    `default_operator` is the deployment's fallback and applies only to the
    direct agent path, where nobody has signed in and there is no principal to
    derive an alias from. It is accepted here so that one call site can serve
    both paths.
    """
    return principal.acts_as or principal.username or default_operator


def with_alias(principal: Principal, alias: str) -> Principal:
    """A copy that records the alias its work actually ran as, for evidence."""
    return replace(principal, acts_as=alias)


def with_branch(principal: Principal, branch: str) -> Principal:
    """A copy signed in at `branch`.

    Used by the console at sign-in and by the service when it falls back to the
    operator's configured branch, so that both paths reach `principal.json`
    through the same field rather than one of them stamping the evidence
    directly.
    """
    return replace(principal, branch=branch)
