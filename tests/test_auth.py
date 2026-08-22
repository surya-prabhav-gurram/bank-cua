"""
Sign-in, and the two questions authorisation actually asks.

The system already refused to take orders from the caller about risk, approval
and operator identity. What it could not do was tell WHO was asking -- so a
teller and a supervisor were the same anonymous client. This is the module that
supplies the subject, and these tests are mostly about the two things that must
be true of it:

  * a session token is a statement about IDENTITY and never about permission,
    so forging a permission means forging an identity a store also recognises;
  * this console admits the institution's own operators and nobody else, and
    that is enforced in the STORE rather than by the absence of a form -- a
    principal file that names a member cannot produce a member session.
"""
import time

import pytest

from bankcua.auth import (AuthError, DEFAULT_PRINCIPAL_ROLES, Principal,
                          PrincipalStore, SessionAuthority, SessionSigner,
                          STAFF_ROLES, hash_password, may_invoke,
                          operator_alias_for, token_from_request,
                          verify_password)

TELLER = Principal(username="teller1", role="teller", acts_as="teller1")
SUPER = Principal(username="super1", role="supervisor", acts_as="super1")


@pytest.fixture
def store(tmp_path):
    import json
    path = tmp_path / "principals.json"
    path.write_text(json.dumps({
        "_comment": "ignored",
        "super1": {"kind": "staff", "role": "supervisor", "acts_as": "super1",
                   "password_hash": hash_password("password", rounds=1000)},
        "teller1": {"kind": "staff", "role": "teller", "acts_as": "teller1",
                    "password_hash": hash_password("password", rounds=1000)},
        "100234": {"kind": "member", "role": "member", "member_id": "100234",
                   "password_hash": hash_password("member123", rounds=1000)},
        "plaintext1": {"kind": "staff", "role": "teller",
                       "password": "hand-edited"},
        "roleless": {"kind": "staff"},
    }))
    return PrincipalStore(str(path))


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def test_a_password_round_trips_and_a_wrong_one_does_not():
    encoded = hash_password("correct horse", rounds=1000)
    assert encoded.startswith("pbkdf2_sha256$")
    assert "correct horse" not in encoded
    assert verify_password("correct horse", encoded)
    assert not verify_password("correct horses", encoded)


def test_a_hand_edited_plaintext_entry_still_works(store):
    """A reader must be able to change a demo password without running a tool.

    Accepted deliberately, and compared the same way -- the cost is at-rest
    exposure, not a weaker check.
    """
    assert store.authenticate("plaintext1", "hand-edited").role == "teller"
    with pytest.raises(AuthError):
        store.authenticate("plaintext1", "wrong")


def test_an_unknown_sign_in_and_a_wrong_password_answer_identically(store):
    """The form must not confirm which sign-ins are live.

    The operator names on this console are few and guessable, so the list is not
    a secret -- but which entries actually resolve should not be free to
    discover by typing at the form.
    """
    with pytest.raises(AuthError) as unknown:
        store.authenticate("nobody1", "password")
    with pytest.raises(AuthError) as wrong:
        store.authenticate("teller1", "not-it")
    assert str(unknown.value) == str(wrong.value)


def test_a_member_entry_cannot_become_a_session(store):
    """The "staff only" claim is enforced in the STORE, not by the login page.

    This console has no member sign-in. That has to hold against a principal
    file somebody hand-edited a member back into -- otherwise the guarantee is
    only that the form has no second box on it, which "view source" undoes. The
    fixture still contains member 100234 with a correct password precisely so
    that this asserts a refusal rather than an absence.
    """
    with pytest.raises(AuthError) as ex:
        store.get("100234")
    assert "staff only" in str(ex.value)

    # ...and not merely at `get`: the password is right, and it still fails.
    with pytest.raises(AuthError):
        store.authenticate("100234", "member123")


def test_an_entry_with_no_role_is_refused_rather_than_defaulted(store):
    """A missing role must not read as "whatever the default happens to be"."""
    with pytest.raises(AuthError) as ex:
        store.get("roleless")
    assert "staff only" in str(ex.value)


def test_the_sign_in_list_skips_what_it_cannot_resolve(store):
    """`usernames()` reads the file; the console renders only what `get`
    accepts, so a member left in the store is invisible rather than offered."""
    resolvable = []
    for name in store.usernames():
        try:
            resolvable.append(store.get(name).username)
        except AuthError:
            continue
    assert "100234" not in resolvable
    assert {"super1", "teller1"} <= set(resolvable)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def test_a_token_carries_an_identity_and_no_permissions():
    """The payload is a username and an expiry.

    If a token carried a role, then compromising the signing key once would
    hand out supervisor sessions for accounts that do not exist. Carrying only
    a name means a forged token still has to name somebody the store knows.
    """
    import base64
    import json
    signer = SessionSigner(b"k")
    payload = signer.mint(TELLER).split(".")[0]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert set(claims) == {"u", "iat", "exp"}
    assert claims["u"] == "teller1"


def test_a_tampered_or_foreign_token_does_not_verify():
    good = SessionSigner(b"key-one").mint(SUPER)
    with pytest.raises(AuthError):
        SessionSigner(b"key-two").verify(good)          # another deployment's key
    payload, _, sig = good.partition(".")
    with pytest.raises(AuthError):
        SessionSigner(b"key-one").verify(payload + "." + sig[:-2] + "xy")


def test_an_expired_session_is_refused():
    signer = SessionSigner(b"k", ttl_s=10)
    token = signer.mint(TELLER, now=time.time() - 3600)
    with pytest.raises(AuthError) as ex:
        signer.verify(token)
    assert "expired" in str(ex.value)


def test_a_session_is_re_resolved_against_the_store_on_every_use(store, tmp_path):
    """A demotion must take effect on the next request, not the next sign-in.

    This is the whole reason the token holds no role: the authority looks the
    username up again, so editing the store is enough to change what an already
    issued session can do.
    """
    import json
    authority = SessionAuthority(store=store, signer=SessionSigner(b"k"))
    principal, token = authority.sign_in("super1", "password")
    assert principal.role == "supervisor"

    raw = json.loads(open(store.path).read())
    raw["super1"]["role"] = "teller"
    open(store.path, "w").write(json.dumps(raw))

    assert authority.principal(token).role == "teller"


def test_a_token_is_never_read_from_a_query_string():
    """Header first, then cookie. A token in a URL is a token in an access log,
    a referrer header, and any screenshot of the address bar."""
    assert token_from_request({"X-Bankcua-Session": "abc"}, {}) == "abc"
    assert token_from_request({"Authorization": "Bearer xyz"}, {}) == "xyz"
    assert token_from_request({}, {"bankcua_session": "ck"}) == "ck"
    assert token_from_request({}, {}) == ""


# ---------------------------------------------------------------------------
# Role gate
# ---------------------------------------------------------------------------
def ROLE_DENIED(denial):
    return denial is not None and denial.code == "CAPABILITY_NOT_PERMITTED_FOR_ROLE"


def test_the_default_role_set_is_the_configured_operator_roles():
    """An unconfigured capability falls back to the operator roles, and to
    nothing wider. There is no role outside STAFF_ROLES that a fallback could
    admit, which is the property worth pinning: adding a role to the system
    must be a deliberate edit here and not a side effect elsewhere."""
    assert DEFAULT_PRINCIPAL_ROLES == list(STAFF_ROLES)
    assert may_invoke(TELLER, []) is None
    assert may_invoke(SUPER, []) is None


def test_a_teller_is_refused_a_supervisor_capability():
    """The narrower of the two axes, and the one `place_hold` relies on."""
    assert ROLE_DENIED(may_invoke(TELLER, ["supervisor"]))
    assert may_invoke(SUPER, ["supervisor"]) is None


def test_the_role_gate_reads_the_sign_in_not_the_alias():
    """`allowed_principal_roles` is about the SIGN-IN. A principal whose alias
    happens to be a supervisor's is still gated on the role they signed in
    with -- otherwise the two axes in config/service.yaml would collapse into
    one and `place_hold`'s supervisor-sign-in requirement would be moot."""
    impostor = Principal(username="teller1", role="teller", acts_as="super1")
    assert ROLE_DENIED(may_invoke(impostor, ["supervisor"]))


# ---------------------------------------------------------------------------
# Which alias the work runs as
# ---------------------------------------------------------------------------
def test_every_principal_acts_as_itself():
    """Nothing here consults the request, which is what makes the service's
    OPERATOR_NOT_SESSION refusal enforceable: a teller's session resolves to
    `teller1` no matter what a request body asks for."""
    assert operator_alias_for(TELLER, "teller1") == "teller1"
    assert operator_alias_for(SUPER, "teller1") == "super1"


def test_the_alias_falls_back_to_the_username_before_the_default():
    """`default_operator` belongs to the direct agent path, where nobody has
    signed in. A signed-in principal must never silently borrow it."""
    unaliased = Principal(username="super1", role="supervisor")
    assert operator_alias_for(unaliased, "teller1") == "super1"
