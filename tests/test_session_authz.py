"""
What a SIGN-IN is allowed to ask the capability API for.

The API already refused to let a request decide its own risk, approval or
operator. What these tests cover is the layer above that: once a person is
signed in, the API stops taking the operator alias from the request at all, and
starts asking a question the request cannot answer for itself: may this ROLE use
this capability.

Every assertion below is a REFUSAL, deliberately: each one is answered before a
browser is launched, so a request that should not happen never reaches a
member's account at all. That ordering is the property, not an optimisation.

The store fixture still contains a member entry, and the tests keep it there on
purpose: this console signs in the institution's own operators only, and that
has to hold against a principal file someone hand-edited rather than merely
against a login page with one form on it.
"""
import json
import os

import pytest

from bankcua.auth import (PrincipalStore, SessionAuthority, SessionSigner,
                          hash_password)
from bankcua.safety.credentials import OperatorIdentity, StaticCredentialStore
from bankcua.service import create_app

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
CATALOG = os.path.join(ROOT, "capabilities", "meridian")

BRANCH = {"branch": "MAIN-001"}
CREDS = StaticCredentialStore({
    "teller1": OperatorIdentity("teller1", "teller", {"password": "pw"}, BRANCH),
    "super1": OperatorIdentity("super1", "supervisor", {"password": "pw"}, BRANCH),
})


def _skip_without_catalog():
    if not os.path.isdir(CATALOG) or not os.listdir(CATALOG):
        pytest.skip("meridian capabilities not recorded")


@pytest.fixture
def authority(tmp_path):
    path = tmp_path / "principals.json"
    path.write_text(json.dumps({
        "super1": {"kind": "staff", "role": "supervisor", "acts_as": "super1",
                   "password_hash": hash_password("password", rounds=1000)},
        "teller1": {"kind": "staff", "role": "teller", "acts_as": "teller1",
                    "password_hash": hash_password("password", rounds=1000)},
        "100234": {"kind": "member", "role": "member", "member_id": "100234",
                   "password_hash": hash_password("member123", rounds=1000)},
    }))
    return SessionAuthority(store=PrincipalStore(str(path)),
                            signer=SessionSigner(b"test-key"))


def _make(tmp_path, authority, require_session=False):
    _skip_without_catalog()
    cfg = tmp_path / "service.yaml"
    cfg.write_text(json.dumps({
        "default_operator": "teller1",
        "capabilities": {
            "meridian.member_lookup": {
                "allow_unapproved": True,
                "allowed_principal_roles": ["supervisor", "teller"]},
            "meridian.member_search": {
                "allow_unapproved": True,
                "allowed_principal_roles": ["supervisor", "teller"]},
            "meridian.transfer_funds": {
                "allow_risky": True, "allow_unapproved": True,
                "allowed_principal_roles": ["supervisor", "teller"]},
            "meridian.place_hold": {
                "requires_role": "supervisor", "allowed_operators": ["super1"],
                "allow_unapproved": True,
                "allowed_principal_roles": ["supervisor"]},
        }}))
    app = create_app(catalog_dir=CATALOG, service_config_path=str(cfg),
                     evidence_dir=str(tmp_path / "ev"), credential_store=CREDS,
                     session_authority=authority,
                     # See test_service.py: the default inbox is the repo's own
                     # committed evidence directory.
                     handoff_dir=str(tmp_path / "handoffs"),
                     require_session=require_session or None)
    return app.test_client()


def _as(authority, username):
    return {"X-Bankcua-Session": authority.mint(authority.store.get(username))}


@pytest.fixture
def client(tmp_path, authority):
    return _make(tmp_path, authority)


# ---------------------------------------------------------------------------
# The role gate
# ---------------------------------------------------------------------------
def test_a_teller_sign_in_cannot_reach_a_supervisor_capability(client, authority):
    r = client.post("/invoke/meridian.place_hold",
                    headers=_as(authority, "teller1"),
                    json={"params": {"member_id": "100234",
                                     "share_id": "100234-S0070",
                                     "reason_code": "FRAUD"}})
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] == "CAPABILITY_NOT_PERMITTED_FOR_ROLE"


def test_a_member_entry_cannot_be_signed_in_or_minted_a_session(authority):
    """There is no member sign-in, and that is enforced in the STORE.

    The fixture deliberately contains member 100234 with a valid password hash.
    Resolving it must fail, which means no session can be minted for it -- so
    there is no member-shaped caller for any of the routes below to have to
    defend against, and the guarantee does not depend on the login page.
    """
    from bankcua.auth import AuthError

    with pytest.raises(AuthError) as ex:
        authority.store.get("100234")
    assert "staff only" in str(ex.value)

    with pytest.raises(AuthError):
        authority.store.authenticate("100234", "member123")


def test_a_session_for_a_member_username_is_refused_at_the_api(client):
    """And if one were minted anyway -- a leaked signing key -- the API still
    re-resolves the username against the store on every call, so the token
    names somebody the store will not return."""
    forged = SessionSigner(b"test-key").mint("100234")
    r = client.post("/invoke/meridian.member_lookup",
                    headers={"X-Bankcua-Session": forged},
                    json={"params": {"member_id": "100234"}})
    assert r.status_code == 401


def test_what_the_engine_is_actually_handed(tmp_path, authority, monkeypatch):
    """The positive control, with the browser stubbed out.

    The property only shows up in the parameters the engine RECEIVES: an
    operator's request about any member is passed through untouched -- looking
    up somebody else's member number is the job -- while the operator alias is
    the one the SESSION resolves to and not one the request chose.
    """
    import bankcua.service as service
    from bankcua.replay.result import ReplayResult, ReplayStatus

    seen = {}

    class _FakeSurface:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass

    class _FakeEngine:
        def __init__(self, *a, **kw): pass
        def run(self, art, params):
            seen.clear()
            seen.update(params)
            return ReplayResult(status=ReplayStatus.SUCCESS, capability_id=art.id)

    monkeypatch.setattr(service, "WebSurface", _FakeSurface)
    monkeypatch.setattr(service, "ReplayEngine", _FakeEngine)
    client = _make(tmp_path, authority)

    client.post("/invoke/meridian.member_lookup",
                headers=_as(authority, "teller1"),
                json={"params": {"member_id": "100987"}})
    assert seen["member_id"] == "100987", "an operator is not scoped to one member"
    assert seen["operator"] == "teller1"

    client.post("/invoke/meridian.member_lookup",
                headers=_as(authority, "super1"),
                json={"params": {"member_id": "100987"}})
    assert seen["member_id"] == "100987"
    assert seen["operator"] == "super1", (
        "the alias comes from the sign-in, so a supervisor's run is recorded "
        "and executed as super1 rather than as the deployment default")


def test_the_subject_is_recorded_next_to_the_evidence(tmp_path, authority,
                                                      monkeypatch):
    """Who asked is written beside the run, not into the result contract.

    The contract describes what the bank answered; a subject is not part of that
    answer. Recording it separately is what an auditor reads to see which
    sign-in stood behind an action, and which Meridian operator it ran as.
    """
    import bankcua.service as service
    from bankcua.replay.result import ReplayResult, ReplayStatus

    class _FakeSurface:
        def __init__(self, *a, **kw): pass
        def start(self): pass
        def stop(self): pass

    class _FakeEngine:
        def __init__(self, *a, **kw): pass
        def run(self, art, params):
            return ReplayResult(status=ReplayStatus.SUCCESS, capability_id=art.id)

    monkeypatch.setattr(service, "WebSurface", _FakeSurface)
    monkeypatch.setattr(service, "ReplayEngine", _FakeEngine)
    client = _make(tmp_path, authority)
    body = client.post("/invoke/meridian.member_lookup",
                       headers=_as(authority, "super1"),
                       json={"params": {"member_id": "100234"}}).get_json()

    recorded = json.load(open(os.path.join(
        str(tmp_path / "ev"), body["run_id"], "principal.json")))
    assert recorded["username"] == "super1"
    assert recorded["role"] == "supervisor"
    assert recorded["runs_as"] == "super1"
    assert "password" not in json.dumps(recorded).lower()


# ---------------------------------------------------------------------------
# Identity cannot be re-chosen by the request
# ---------------------------------------------------------------------------
def test_a_signed_in_caller_cannot_name_a_different_operator(client, authority):
    """The escalation this closes: a teller's session naming `super1`. The
    credential store stopped a caller sending a password; this stops a caller
    choosing whose password gets sent."""
    r = client.post("/invoke/meridian.member_lookup",
                    headers=_as(authority, "teller1"),
                    json={"operator": "super1", "params": {"member_id": "100234"}})
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] == "OPERATOR_NOT_SESSION"


def test_a_forged_session_is_refused_rather_than_ignored(client):
    """A token that does not verify is a different event from no token at all.
    Treating both as 'anonymous' is how a revoked session keeps working."""
    forged = SessionSigner(b"not-the-key").mint("super1")
    r = client.post("/invoke/meridian.member_lookup",
                    headers={"X-Bankcua-Session": forged},
                    json={"params": {"member_id": "100234"}})
    assert r.status_code == 401
    assert r.get_json()["refusal"]["code"] == "SESSION_INVALID"


def test_a_session_naming_nobody_the_store_knows_is_refused(client):
    """Signed correctly, but for a principal that does not exist -- which is
    what a leaked key buys an attacker, and why the token carries no role."""
    r = client.post("/invoke/meridian.member_lookup",
                    headers={"X-Bankcua-Session":
                             SessionSigner(b"test-key").mint("nobody")},
                    json={"params": {}})
    assert r.status_code == 401


def test_the_direct_agent_path_still_works_when_sessions_are_not_required(client):
    """Non-browser callers name an operator alias and get the pre-existing
    server-side rules. Breaking that would break every CLI demo in the README
    to add a browser sign-in they cannot perform."""
    r = client.post("/invoke/meridian.place_hold",
                    json={"operator": "teller1",
                          "params": {"member_id": "100234",
                                     "share_id": "100234-S0070",
                                     "reason_code": "FRAUD"}})
    # Refused on the OPERATOR's role, as it always was -- not on a session.
    assert r.get_json()["refusal"]["code"] in ("ROLE_NOT_PERMITTED",
                                               "OPERATOR_NOT_PERMITTED")


def test_with_sessions_required_an_anonymous_call_is_refused(tmp_path, authority):
    client = _make(tmp_path, authority, require_session=True)
    r = client.post("/invoke/meridian.member_lookup",
                    json={"operator": "teller1", "params": {"member_id": "100234"}})
    assert r.status_code == 401
    assert r.get_json()["refusal"]["code"] == "SESSION_REQUIRED"


# ---------------------------------------------------------------------------
# The published action space
# ---------------------------------------------------------------------------
def test_the_manifest_is_narrowed_to_what_the_sign_in_may_invoke(client,
                                                                 authority):
    """The manifest IS a chatbot's action space, so a capability this operator
    may not use is best not described to the model routing for them."""
    everything = {c["name"] for c in client.get("/capabilities").get_json()}
    as_teller = {c["name"] for c in client.get(
        "/capabilities", headers=_as(authority, "teller1")).get_json()}
    as_super = {c["name"] for c in client.get(
        "/capabilities", headers=_as(authority, "super1")).get_json()}
    assert "meridian.place_hold" in everything
    assert "meridian.place_hold" not in as_teller
    assert "meridian.place_hold" in as_super
    assert "meridian.member_lookup" in as_teller


def test_the_manifest_still_asks_an_operator_which_member_they_mean(client,
                                                                    authority):
    """`member_id` is the operator's decision and must stay in the schema.

    It is the counterpart to the narrowing above: the manifest hides what a
    caller may not use, and asks for everything a caller must still choose.
    """
    tools = {c["name"]: c for c in client.get(
        "/capabilities", headers=_as(authority, "teller1")).get_json()}
    schema = tools["meridian.member_lookup"]["input_schema"]
    assert "member_id" in (schema.get("properties") or {})


def test_the_operator_list_collapses_to_the_one_the_session_acts_as(client,
                                                                    authority):
    """Once someone is signed in there is no choice left to offer: the alias
    comes from the sign-in, so listing the others invites a request that would
    only be refused with OPERATOR_NOT_SESSION."""
    assert [o["alias"] for o in client.get(
        "/operators", headers=_as(authority, "teller1")).get_json()] == ["teller1"]
    assert [o["alias"] for o in client.get(
        "/operators", headers=_as(authority, "super1")).get_json()] == ["super1"]
    assert len(client.get("/operators").get_json()) == 2


def test_the_session_endpoint_reports_the_subject_and_no_secret(client,
                                                                authority):
    body = client.get("/session", headers=_as(authority, "teller1")).get_json()
    assert body["role"] == "teller"
    assert body["runs_as"] == "teller1"
    assert "password" not in json.dumps(body).lower()
    assert client.get("/session").status_code == 401
