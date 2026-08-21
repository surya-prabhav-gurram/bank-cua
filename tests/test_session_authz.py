"""
What a SIGN-IN is allowed to ask the capability API for.

The API already refused to let a request decide its own risk, approval or
operator. What these tests cover is the layer above that: once a person is
signed in, the API stops taking the operator alias from the request at all, and
starts asking two questions the request cannot answer for itself -- may this
ROLE use this capability, and whose records may it touch.

Every assertion below is a REFUSAL, deliberately: each one is answered before a
browser is launched, so a request that should not happen never reaches a
member's account at all. That ordering is the property, not an optimisation.
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
                "allowed_principal_roles": ["supervisor", "teller", "member"]},
            "meridian.member_search": {
                "allow_unapproved": True,
                "allowed_principal_roles": ["supervisor", "teller"]},
            "meridian.transfer_funds": {
                "allow_risky": True, "allow_unapproved": True,
                "allowed_principal_roles": ["supervisor", "teller", "member"]},
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


def test_a_member_sign_in_cannot_reach_a_staff_capability(client, authority):
    """Searching the membership by surname is a directory lookup over other
    people's records: there is no version of it scoped to one member, so it is
    refused rather than narrowed."""
    r = client.post("/invoke/meridian.member_search",
                    headers=_as(authority, "100234"),
                    json={"params": {"last_name": "Turing"}})
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] == "CAPABILITY_NOT_PERMITTED_FOR_ROLE"


def test_delegation_does_not_hand_a_member_a_tellers_action_space(client,
                                                                  authority):
    """A member's work EXECUTES as teller1. If the gate looked at the alias
    instead of the sign-in, that alone would grant them everything a teller can
    do -- which is the failure this separation exists to prevent."""
    r = client.post("/invoke/meridian.place_hold",
                    headers=_as(authority, "100234"),
                    json={"params": {"share_id": "100234-S0070",
                                     "reason_code": "FRAUD"}})
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] == "CAPABILITY_NOT_PERMITTED_FOR_ROLE"


# ---------------------------------------------------------------------------
# Member scope
# ---------------------------------------------------------------------------
def test_a_member_asking_about_another_member_is_refused(client, authority):
    r = client.post("/invoke/meridian.member_lookup",
                    headers=_as(authority, "100234"),
                    json={"params": {"member_id": "100987"}})
    assert r.status_code == 403
    body = r.get_json()
    assert body["refusal"]["code"] == "MEMBER_SCOPE_VIOLATION"
    assert "100987" in body["refusal"]["reason"]


def test_a_member_cannot_move_money_to_another_members_share(client, authority):
    """The one that matters most: a scope check that covered only `member_id`
    would let 'transfer from my share to 100987-S0001' through with a perfectly
    correct member number attached."""
    r = client.post("/invoke/meridian.transfer_funds",
                    headers=_as(authority, "100234"),
                    json={"params": {"from_share": "100234-S0070",
                                     "to_share": "100987-S0001",
                                     "amount": "50.00"}})
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] == "MEMBER_SCOPE_VIOLATION"


def test_what_the_engine_is_actually_handed(tmp_path, authority, monkeypatch):
    """The positive controls, with the browser stubbed out.

    Two properties that only show up in the parameters the engine RECEIVES:
    a teller's request about another member is passed through untouched (or the
    console would be useless to staff), and a member's request arrives carrying
    their own member number even though nothing in the request named one.
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
    assert seen["member_id"] == "100987", "staff are not scoped to one member"
    assert seen["operator"] == "teller1"

    client.post("/invoke/meridian.member_lookup",
                headers=_as(authority, "100234"), json={"params": {}})
    assert seen["member_id"] == "100234", (
        "a member's own number must be filled in from the sign-in, or the "
        "assistant has to ask a member who they are")
    # ...and it still runs as a real Meridian operator, because a member has no
    # back-office identity of their own.
    assert seen["operator"] == "teller1"


def test_the_subject_is_recorded_next_to_the_evidence(tmp_path, authority,
                                                      monkeypatch):
    """Who asked is written beside the run, not into the result contract.

    The contract describes what the bank answered; a subject is not part of that
    answer. Recording it separately is what lets the console show a member their
    own runs and nobody else's, and what an auditor reads to see which sign-in
    stood behind an action.
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
                       headers=_as(authority, "100234"),
                       json={"params": {}}).get_json()

    recorded = json.load(open(os.path.join(
        str(tmp_path / "ev"), body["run_id"], "principal.json")))
    assert recorded["username"] == "100234"
    assert recorded["member_id"] == "100234"
    assert recorded["runs_as"] == "teller1"
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
    """The manifest IS a chatbot's action space, so a capability a member may
    not use is best not described to the model routing for them."""
    everything = {c["name"] for c in client.get("/capabilities").get_json()}
    as_member = {c["name"] for c in client.get(
        "/capabilities", headers=_as(authority, "100234")).get_json()}
    assert "meridian.place_hold" in everything
    assert "meridian.place_hold" not in as_member
    assert "meridian.member_search" not in as_member
    assert "meridian.member_lookup" in as_member


def test_a_members_manifest_does_not_ask_them_which_member_they_are(client,
                                                                    authority):
    """`member_id` comes from the sign-in, so leaving it in the schema would
    leave a field for a chatbot to fill in with somebody else's number."""
    tools = {c["name"]: c for c in client.get(
        "/capabilities", headers=_as(authority, "100234")).get_json()}
    schema = tools["meridian.member_lookup"]["input_schema"]
    assert "member_id" not in (schema.get("properties") or {})
    assert "member_id" not in (schema.get("required") or [])
    # ...and it is still asked of staff, who must decide it
    staff = {c["name"]: c for c in client.get(
        "/capabilities", headers=_as(authority, "teller1")).get_json()}
    assert "member_id" in staff["meridian.member_lookup"]["input_schema"]["properties"]


def test_the_operator_list_collapses_to_the_one_the_session_acts_as(client,
                                                                    authority):
    """Once someone is signed in there is no choice left to offer. Showing a
    member the institution's operator names would disclose them for nothing."""
    assert [o["alias"] for o in client.get(
        "/operators", headers=_as(authority, "teller1")).get_json()] == ["teller1"]
    assert [o["alias"] for o in client.get(
        "/operators", headers=_as(authority, "100234")).get_json()] == ["teller1"]
    assert len(client.get("/operators").get_json()) == 2


def test_the_session_endpoint_reports_the_subject_and_no_secret(client,
                                                                authority):
    body = client.get("/session", headers=_as(authority, "100234")).get_json()
    assert body["role"] == "member" and body["member_id"] == "100234"
    assert body["runs_as"] == "teller1"
    assert "password" not in json.dumps(body).lower()
    assert client.get("/session").status_code == 401


def test_a_member_cannot_counter_sign_their_own_request(client, authority):
    """`approver` clears the dual-control gate on a large transfer.

    The engine checks that a counter-signature is INDEPENDENT of the initiator
    -- but a member's work is initiated by the delegated teller alias, so a
    member naming a supervisor here would look independent and would authorise
    their own transfer with nobody having approved it. The field was always
    caller-supplied, which was defensible while every caller was the
    institution.
    """
    r = client.post("/invoke/meridian.transfer_funds",
                    headers=_as(authority, "100234"),
                    json={"approver": "super1",
                          "params": {"from_share": "100234-S0070",
                                     "to_share": "100234-S0001-3",
                                     "amount": "2000.00"}})
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] == "FIELD_NOT_PERMITTED_FOR_ROLE"


def test_a_member_cannot_re_point_a_capability_at_another_tenant(client,
                                                                 authority):
    """Which deployment of the vendor product this runs against is an
    integration decision, not a member's."""
    r = client.post("/invoke/meridian.member_lookup",
                    headers=_as(authority, "100234"),
                    json={"tenant": {"tenant_id": "elsewhere",
                                     "base_url": "https://elsewhere.example"},
                          "params": {}})
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] == "FIELD_NOT_PERMITTED_FOR_ROLE"
