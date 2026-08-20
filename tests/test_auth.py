"""
Sign-in, and the two questions authorisation actually asks.

The system already refused to take orders from the caller about risk, approval
and operator identity. What it could not do was tell WHO was asking -- so a
member of the public and a supervisor were the same anonymous client. This is the
module that supplies the subject, and these tests are mostly about the two
things that must be true of it:

  * a session token is a statement about IDENTITY and never about permission,
    so forging a permission means forging an identity a store also recognises;
  * a member's request is bound to the member's own record before it goes
    anywhere, and a request that cannot be bound is refused rather than
    quietly retargeted.
"""
import time

import pytest

from bankcua.auth import (AuthError, DEFAULT_PRINCIPAL_ROLES, Principal,
                          PrincipalStore, SessionAuthority, SessionSigner,
                          hash_password, may_invoke, operator_alias_for,
                          scope_params, token_from_request, verify_password)

MEMBER = Principal(username="100234", role="member", kind="member",
                   member_id="100234")
TELLER = Principal(username="teller1", role="teller", kind="staff",
                   acts_as="teller1")
SUPER = Principal(username="super1", role="supervisor", kind="staff",
                  acts_as="super1")


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
        "broken": {"kind": "member", "role": "member"},
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
    """Member usernames ARE member numbers.

    A store that distinguished "no such sign-in" from "wrong password" would
    confirm which member numbers exist to anyone who typed one, which is an
    enumeration of the membership.
    """
    with pytest.raises(AuthError) as unknown:
        store.authenticate("999999", "member123")
    with pytest.raises(AuthError) as wrong:
        store.authenticate("100234", "not-it")
    assert str(unknown.value) == str(wrong.value)


def test_a_member_principal_with_no_member_id_is_refused_not_defaulted(store):
    """Scoped to nothing must not read as scoped to everything."""
    with pytest.raises(AuthError) as ex:
        store.get("broken")
    assert "member_id" in str(ex.value)


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
def test_an_unconfigured_capability_is_closed_to_members():
    """Fail closed, and closed specifically against the public.

    A capability nobody has written a rule for must not become member-reachable
    by omission -- which is the failure mode that matters, because the ones
    people forget are the new ones.
    """
    assert MEMBER_ROLE_DENIED(may_invoke(MEMBER, []))
    assert may_invoke(TELLER, []) is None
    assert "member" not in DEFAULT_PRINCIPAL_ROLES


def MEMBER_ROLE_DENIED(denial):
    return denial is not None and denial.code == "CAPABILITY_NOT_PERMITTED_FOR_ROLE"


def test_the_role_gate_is_about_the_sign_in_not_the_delegated_alias():
    """A member's work runs as a teller alias. That must not hand them a
    teller's action space, or delegation would be a privilege grant."""
    delegated = Principal(username="100234", role="member", kind="member",
                          member_id="100234", acts_as="teller1")
    assert MEMBER_ROLE_DENIED(may_invoke(delegated, ["supervisor", "teller"]))
    assert may_invoke(delegated, ["supervisor", "teller", "member"]) is None


def test_a_teller_is_refused_a_supervisor_capability():
    assert MEMBER_ROLE_DENIED(may_invoke(TELLER, ["supervisor"]))
    assert may_invoke(SUPER, ["supervisor"]) is None


# ---------------------------------------------------------------------------
# Member scope
# ---------------------------------------------------------------------------
def test_staff_requests_pass_through_untouched():
    """A teller looking up somebody else's member number is the job."""
    params, denial = scope_params(TELLER, {"member_id": "100987"})
    assert denial is None and params["member_id"] == "100987"


def test_a_members_own_number_is_filled_in_rather_than_asked_for():
    params, denial = scope_params(MEMBER, {})
    assert denial is None and params["member_id"] == "100234"


def test_another_members_number_is_refused_not_rewritten():
    """Silently retargeting the request onto their own account would be worse
    than declining it: 'transfer to 100987' would move money somewhere the
    person did not ask for and would report success."""
    params, denial = scope_params(MEMBER, {"member_id": "100987"})
    assert denial is not None and denial.code == "MEMBER_SCOPE_VIOLATION"
    assert params.get("member_id") == "100987"      # not rewritten


def test_a_share_belonging_to_another_member_is_refused():
    _p, denial = scope_params(MEMBER, {"from_share": "100234-S0070",
                                       "to_share": "100987-S0001"})
    assert denial is not None and denial.code == "MEMBER_SCOPE_VIOLATION"
    assert "100987-S0001" in denial.reason

    _p, ok = scope_params(MEMBER, {"from_share": "100234-S0070",
                                   "to_share": "100234-S0001-3"})
    assert ok is None


def test_a_foreign_member_number_in_any_parameter_is_refused():
    """The catch-all. A parameter added to a capability tomorrow, which nobody
    thought to list, must not be able to carry another member's number."""
    _p, denial = scope_params(MEMBER, {"beneficiary": "100987"})
    assert denial is not None and denial.code == "MEMBER_SCOPE_VIOLATION"
    # ...while free text that merely contains digits is not an identifier
    _p, ok = scope_params(MEMBER, {"memo": "invoice 100987-A for the roof"})
    assert ok is None


def test_a_member_sign_in_bound_to_nothing_can_do_nothing():
    unbound = Principal(username="ghost", role="member", kind="member")
    _p, denial = scope_params(unbound, {"member_id": "100234"})
    assert denial is not None and denial.code == "MEMBER_SCOPE_UNKNOWN"


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------
def test_a_member_runs_as_the_deployments_least_privileged_alias():
    """A member has no back-office identity, so their work is executed by one --
    which is safe only in combination with the scoping above."""
    assert operator_alias_for(MEMBER, "teller1") == "teller1"
    assert operator_alias_for(TELLER, "teller1") == "teller1"
    assert operator_alias_for(SUPER, "teller1") == "super1"
