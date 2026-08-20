"""
Who is signed in, and what that identity is allowed to ask for.

The decision this file exists to contain
----------------------------------------
Everything downstream of here already refuses to take orders from the caller:
`config/service.yaml` decides whether a capability may perform its irreversible
step, `bankcua/safety/credentials.py` decides which secret an operator ALIAS
resolves to, and the replay engine decides what a capability is allowed to do to
a page. What none of them knew until now is WHO IS SITTING IN FRONT OF THE
CONSOLE -- so a chatbot window opened by a member of the public and one opened by
a supervisor were the same caller.

This module supplies that missing subject, and it does so in a way that cannot be
asserted by the request:

  * a person signs in ONCE, against a principal store the deployment configures;
  * the console mints a signed, expiring session token that carries a USERNAME
    and nothing else -- no role, no member id, no operator alias, because a claim
    the token carried would be a claim an attacker only has to forge once;
  * every consumer (the portal, the dashboard, the capability API) re-resolves
    that username against the store, so a role change or a deletion takes effect
    on the next request rather than at the next sign-in;
  * the operator ALIAS a run executes as is derived from the principal, never
    read from the request body. A teller's session cannot name `super1`.

Three kinds of subject, and why members are different in kind
-------------------------------------------------------------
Staff principals (`supervisor`, `teller`) map onto a Meridian operator: they are
the bank's own users, and the alias they act as is their own.

A member is NOT a Meridian operator and never will be -- there is no member login
on the back office. A member session therefore runs DELEGATED: the work is
executed by the deployment's least-privileged staff alias, on the member's behalf
and hard-bound to the member's own record by `scope_params`. That is the whole
safety argument for letting a member near this system at all, so it is enforced
here, in one function, and asserted in tests/test_auth.py rather than described
in a docstring.

Note what delegation does NOT grant: acting as a teller alias does not give a
member a teller's action space. Which capabilities each ROLE may invoke is
declared per capability in `config/service.yaml` (`allowed_principal_roles`) and
defaults closed to staff, so a member reaches only what the deployment has
explicitly opened to members -- and only ever their own row of it.

The seam: swap `PrincipalStore` for an IdP, LDAP or the institution's directory
without touching the portal, the dashboard, the chatbot or the API.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, replace

#: Roles that belong to the institution. Everything else is a member of the
#: public, and the difference is not a matter of degree: staff act on the bank's
#: behalf, members act on exactly one record.
STAFF_ROLES = ("supervisor", "teller")
MEMBER_ROLE = "member"

#: What a capability may be invoked by when `config/service.yaml` says nothing.
#: Closed to members deliberately: a capability nobody has thought about yet must
#: not become member-reachable because someone forgot a line of configuration.
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
    to "I do not know who this is" is to stop.
    """


# ---------------------------------------------------------------------------
# Principals
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    """Who is signed in, and the only facts authorisation is allowed to consult.

    `acts_as` is the operator ALIAS whose Meridian identity executes this
    principal's work. For staff it is their own; for a member it is the
    deployment's default staff alias, which is delegation and is why
    `member_id` must be enforced on every single invocation.
    """
    username: str
    role: str
    kind: str = "staff"                 # "staff" | "member"
    display_name: str = ""
    member_id: str | None = None
    acts_as: str | None = None

    @property
    def is_member(self) -> bool:
        return self.kind == "member"

    @property
    def is_staff(self) -> bool:
        return not self.is_member

    def to_public(self) -> dict:
        """What may be shown in a UI. There is nothing else to withhold -- a
        Principal never holds a secret, which is what lets the portal hand it to
        the browser without a filtering step someone can forget."""
        return {"username": self.username, "role": self.role, "kind": self.kind,
                "display_name": self.display_name or self.username,
                "member_id": self.member_id, "acts_as": self.acts_as}


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

        {"super1": {"role": "supervisor", "acts_as": "super1",
                    "password_hash": "pbkdf2_sha256$..."},
         "100234": {"role": "member", "member_id": "100234",
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
        username: every request re-derives role, member id and delegated alias
        from the store, so a token minted before a demotion does not still carry
        the promotion.
        """
        entry = self._load().get(username)
        if entry is None:
            raise AuthError(f"unknown sign-in {username!r}")
        role = str(entry.get("role") or MEMBER_ROLE)
        kind = str(entry.get("kind") or
                   ("staff" if role in STAFF_ROLES else "member"))
        member_id = entry.get("member_id")
        if kind == "member" and not member_id:
            # A member principal with no member id would be scoped to nothing,
            # which `scope_params` would read as "no member named" rather than
            # "all members". Refuse the configuration instead of guessing.
            raise AuthError(
                f"{username!r} is a member sign-in with no member_id; it cannot "
                f"be scoped to a record and will not be signed in")
        return Principal(username=username, role=role, kind=kind,
                         display_name=str(entry.get("display_name") or username),
                         member_id=str(member_id) if member_id else None,
                         acts_as=(str(entry["acts_as"])
                                  if entry.get("acts_as") else None))

    # ---- authenticating --------------------------------------------------
    def authenticate(self, username: str, password: str) -> Principal:
        """Verify a sign-in. Wrong user and wrong password are the same answer.

        Both cost roughly the same time, too: an unknown username still runs a
        PBKDF2 round against a decoy, so the response time does not tell an
        attacker which member numbers are registered. Member usernames ARE
        member numbers, so that oracle would enumerate the membership.
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

    The payload carries a username and an expiry and NOTHING ELSE. Every other
    fact -- role, member id, which operator alias the work runs as -- is looked
    up server-side at use time. A token is therefore a statement about who
    signed in, never a statement about what they may do, which means forging a
    permission requires forging an identity that a store must also recognise.
    """

    def __init__(self, secret: bytes, ttl_s: float = DEFAULT_TTL_S):
        self.secret = secret
        self.ttl_s = ttl_s

    def mint(self, principal: Principal | str, *, now: float | None = None) -> str:
        username = (principal.username if isinstance(principal, Principal)
                    else str(principal))
        issued = time.time() if now is None else now
        payload = _b64(json.dumps({"u": username, "iat": round(issued),
                                   "exp": round(issued + self.ttl_s)},
                                  separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload)}"

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self.secret, payload.encode(),
                             hashlib.sha256).digest())

    def verify(self, token: str, *, now: float | None = None) -> str:
        """Return the username a valid, unexpired token names, or raise."""
        if not token or "." not in token:
            raise AuthError("no session")
        payload, _, signature = token.partition(".")
        if not hmac.compare_digest(self._sign(payload), signature):
            raise AuthError("session signature does not verify")
        try:
            claims = json.loads(_unb64(payload))
        except Exception as ex:
            raise AuthError("malformed session") from ex
        if float(claims.get("exp", 0)) < (time.time() if now is None else now):
            raise AuthError("session expired; sign in again")
        username = str(claims.get("u") or "")
        if not username:
            raise AuthError("session names no principal")
        return username


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

    def mint(self, principal: Principal) -> str:
        return self.signer.mint(principal)

    def principal(self, token: str) -> Principal:
        return self.store.get(self.signer.verify(token))

    def sign_in(self, username: str, password: str) -> tuple[Principal, str]:
        principal = self.store.authenticate(username, password)
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

    Deliberately about the ROLE and not about the operator alias. A member's work
    is executed by a staff alias, so an alias-only check would hand every member
    a teller's action space the moment delegation was introduced.
    """
    roles = list(allowed_roles or DEFAULT_PRINCIPAL_ROLES)
    if principal.role in roles:
        return None
    return Denial(
        "CAPABILITY_NOT_PERMITTED_FOR_ROLE",
        f"a sign-in with one of these roles: {', '.join(sorted(roles))}",
        f"{principal.username!r} is signed in as {principal.role!r}, which "
        f"cannot use this capability")


#: Parameters whose value NAMES a member's account. Meridian writes share ids as
#: MEMBER-SHARE (100234-S0070), so the member number is checkable inside them.
MEMBER_QUALIFIED_PARAMS = ("from_share", "to_share", "share_id", "account_id",
                           "target_share", "source_share")
#: Parameters that ARE a member number.
MEMBER_ID_PARAMS = ("member_id", "member_number", "mid")

_BARE_MEMBER = re.compile(r"^\d{6}$")


def scope_params(principal: Principal, params: dict) -> tuple[dict, Denial | None]:
    """Bind a member's request to the member's own record, or refuse it.

    Staff pass through untouched -- a teller looking up somebody else's member
    number is the job.

    For a member, three rules, in this order:

      1. `member_id`, if they did not supply it, is FILLED IN from their
         sign-in, so the chatbot never has to ask a member who they are. Only
         that one: the other spellings are recognised for CHECKING, because
         filling in an alias a capability happens to use would be guessing at
         its contract;
      2. one they supplied that is not their own is REFUSED, not rewritten.
         Silently retargeting "transfer $500 to 100987" onto their own account
         would be a worse outcome than declining it;
      3. any share id must be prefixed with their member number, and any other
         parameter that is a bare six-digit member number must be theirs. The
         last rule is the catch-all: it means a parameter added to a capability
         tomorrow, which nobody thought to list here, cannot smuggle another
         member's number through.

    Returns the scoped parameters and, if the request cannot be scoped, the
    refusal to answer with.
    """
    if not principal.is_member:
        return dict(params), None
    mine = str(principal.member_id or "")
    if not mine:
        return dict(params), Denial(
            "MEMBER_SCOPE_UNKNOWN", "a member sign-in bound to a member number",
            f"{principal.username!r} is not bound to a member record")

    scoped = dict(params)
    for name in MEMBER_ID_PARAMS:
        supplied = str(scoped.get(name) or "").strip()
        if not supplied:
            if name == "member_id":
                scoped[name] = mine
            continue
        if supplied != mine:
            return scoped, _cross_member(name, supplied, mine)

    for name in MEMBER_QUALIFIED_PARAMS:
        value = str(scoped.get(name) or "").strip()
        if value and not value.startswith(f"{mine}-"):
            return scoped, _cross_member(name, value, mine)

    for name, value in scoped.items():
        text = str(value or "").strip()
        if _BARE_MEMBER.match(text) and text != mine:
            return scoped, _cross_member(name, text, mine)

    return scoped, None


def _cross_member(param: str, supplied: str, mine: str) -> Denial:
    return Denial(
        "MEMBER_SCOPE_VIOLATION",
        f"a request about member {mine}",
        f"{param}={supplied!r} names an account that is not yours; a member "
        f"sign-in can only act on member {mine}")


def operator_alias_for(principal: Principal, default_operator: str = "") -> str:
    """The Meridian alias this principal's work executes as.

    Staff act as themselves. A member has no back-office identity at all, so
    their work runs DELEGATED as the deployment's default alias -- which
    `config/service.yaml` sets to a teller, the least privileged identity that
    can do useful work. Delegation is safe only in combination with
    `scope_params`, and neither is optional.
    """
    return principal.acts_as or (default_operator if principal.is_member
                                 else principal.username)


def with_alias(principal: Principal, alias: str) -> Principal:
    """A copy that records the alias its work actually ran as, for evidence."""
    return replace(principal, acts_as=alias)
