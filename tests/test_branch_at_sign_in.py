"""
The branch a session is signed in at, and how it reaches the evidence.

MERIDIAN's own sign-on screen has a branch field, so `branch` was always a
required input on every capability -- it was simply a CONSTANT, read off the
operator's entry in the credential store. This makes it a choice made at the
door instead, which changes one thing structurally: a branch cannot be
re-derived from the principal store the way a role can, because it is a choice
rather than a property. It therefore rides in the session token.

That is the only claim a token here carries, and these tests are mostly about
the thing that makes it safe to carry: it is re-validated server-side against
`config/service.yaml` on every invocation, so forging it buys a refusal rather
than a free-text entry in somebody else's audit trail. It grants nothing.
"""
import json
import os

import pytest

import bankcua.service as service
from bankcua.auth import (AuthError, PrincipalStore, SessionAuthority,
                          SessionSigner, hash_password)
from bankcua.replay.result import ReplayResult, ReplayStatus
from bankcua.safety.credentials import OperatorIdentity, StaticCredentialStore
from bankcua.service import create_app

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
CATALOG = os.path.join(ROOT, "capabilities", "meridian")

#: MERIDIAN's own branches, as its sign-on dropdown offers them. These codes are
#: the TARGET's, not ours: `branch` is typed into that form, so a code the host
#: does not offer is a run that fails at its first screen. `test_the_configured
#: _branches_are_the_targets_own` below pins the shipped config against them.
BRANCHES = [{"code": "MAIN-001", "name": "Main Office"},
            {"code": "WEST-014", "name": "Westside"},
            {"code": "EAST-022", "name": "Eastgate"}]
CODES = [b["code"] for b in BRANCHES]

#: The operator's OWN branch, from the credential store. Deliberately one of the
#: configured branches and deliberately not the one the tests sign in at, so a
#: fallback and a choice can never be confused for each other.
CONFIGURED = {"branch": "MAIN-001"}
CREDS = StaticCredentialStore({
    "teller1": OperatorIdentity("teller1", "teller", {"password": "pw"}, CONFIGURED),
    "super1": OperatorIdentity("super1", "supervisor", {"password": "pw"}, CONFIGURED),
})


def _skip_without_catalog():
    if not os.path.isdir(CATALOG) or not os.listdir(CATALOG):
        pytest.skip("meridian capabilities not recorded")


@pytest.fixture
def authority(tmp_path):
    path = tmp_path / "principals.json"
    path.write_text(json.dumps({
        "super1": {"role": "supervisor", "acts_as": "super1",
                   "password_hash": hash_password("password", rounds=1000)},
        "teller1": {"role": "teller", "acts_as": "teller1",
                    "password_hash": hash_password("password", rounds=1000)},
    }))
    return SessionAuthority(store=PrincipalStore(str(path)),
                            signer=SessionSigner(b"branch-test-key"))


@pytest.fixture
def seen():
    """Whatever the replay engine was last handed."""
    return {}


@pytest.fixture
def api(tmp_path, authority, seen, monkeypatch, request):
    _skip_without_catalog()
    branches = getattr(request, "param", BRANCHES)
    cfg = tmp_path / "service.yaml"
    cfg.write_text(json.dumps({
        "default_operator": "teller1",
        "branches": branches,
        "capabilities": {
            "meridian.member_lookup": {
                "allow_unapproved": True,
                "allowed_principal_roles": ["supervisor", "teller"]}}}))

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
    app = create_app(catalog_dir=CATALOG, service_config_path=str(cfg),
                     evidence_dir=str(tmp_path / "ev"), credential_store=CREDS,
                     session_authority=authority,
                     handoff_dir=str(tmp_path / "handoffs"))
    return app.test_client()


def _at(authority, username, branch):
    token = authority.mint(authority.store.get(username), branch=branch)
    return {"X-Bankcua-Session": token}


def _lookup(api, headers=None, **params):
    return api.post("/invoke/meridian.member_lookup", headers=headers or {},
                    json={"params": {"member_id": "100234", **params}})


# ---------------------------------------------------------------------------
# The token
# ---------------------------------------------------------------------------
def test_the_branch_is_the_only_thing_the_token_adds(authority):
    """Still a username and an expiry, plus the one value that cannot be
    looked up. If this set ever grows, the argument in `auth.py` about claims
    being non-load-bearing has to be re-made for whatever was added."""
    import base64

    token = authority.mint(authority.store.get("teller1"), branch="WEST-014")
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
    assert set(claims) == {"u", "iat", "exp", "b"}
    assert claims["u"] == "teller1" and claims["b"] == "WEST-014"
    assert "role" not in claims and "supervisor" not in json.dumps(claims)


def test_a_session_with_no_branch_is_byte_identical_to_the_old_shape(authority):
    """An empty branch is OMITTED rather than written as "". A session minted
    on the direct agent path, or by a deployment that offers no choice, is the
    token this system issued before branches existed -- so upgrading does not
    invalidate anybody's session."""
    import base64

    token = authority.mint(authority.store.get("teller1"))
    claims = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
    assert set(claims) == {"u", "iat", "exp"}
    assert authority.principal(token).branch == ""


def test_the_branch_survives_re_resolution_but_the_role_is_still_looked_up(
        authority, tmp_path):
    """The username is re-read from the store on every use; the branch comes
    from the token because there is nowhere else it could come from. Both
    happen in one call, and a demotion still lands immediately."""
    token = authority.mint(authority.store.get("super1"), branch="EAST-022")
    assert authority.principal(token).role == "supervisor"

    raw = json.loads(open(authority.store.path).read())
    raw["super1"]["role"] = "teller"
    open(authority.store.path, "w").write(json.dumps(raw))

    reread = authority.principal(token)
    assert reread.role == "teller", "the role is re-resolved"
    assert reread.branch == "EAST-022", "the branch is carried"


def test_a_tampered_branch_does_not_verify(authority):
    """The branch is inside the signed payload, so editing it invalidates the
    signature. That is the first line of defence; the configured-list check is
    the one that matters, because it holds even if the key leaks."""
    token = authority.mint(authority.store.get("teller1"), branch="WEST-014")
    payload, _, sig = token.partition(".")
    import base64
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["b"] = "EAST-022"
    forged = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":")).encode()).decode().rstrip("=")
    with pytest.raises(AuthError):
        authority.signer.claims(f"{forged}.{sig}")


# ---------------------------------------------------------------------------
# What reaches the host, and the evidence
# ---------------------------------------------------------------------------
def test_the_signed_in_branch_is_what_the_engine_is_handed(api, authority, seen):
    """It overrides the operator's configured branch, which is the whole point:
    the same teller signs in at a different branch tomorrow."""
    _lookup(api, _at(authority, "teller1", "WEST-014"))
    assert seen["branch"] == "WEST-014"
    assert seen["operator"] == "teller1"


def test_the_branch_is_recorded_against_every_task_the_session_performs(
        api, authority, tmp_path):
    """Written beside the evidence by the same act that records who asked, so
    there is no run that has one and not the other."""
    for branch in ("WEST-014", "EAST-022"):
        body = _lookup(api, _at(authority, "super1", branch)).get_json()
        recorded = json.load(open(os.path.join(
            str(tmp_path / "ev"), body["run_id"], "principal.json")))
        assert recorded["branch"] == branch
        assert recorded["username"] == "super1"
        assert recorded["runs_as"] == "super1"


def test_a_caller_cannot_choose_its_own_branch_per_request(api, authority, seen):
    """The same rule as the operator password: identity is merged in AFTER the
    caller's parameters, so a request naming a branch is ignored rather than
    honoured. An audit field a caller can set is not evidence."""
    _lookup(api, _at(authority, "teller1", "WEST-014"), branch="EAST-022")
    assert seen["branch"] == "WEST-014"


def test_the_branch_is_not_asked_for_in_the_manifest(api, authority):
    """It is service-supplied, so a model routing for this operator is never
    invited to fill it in -- the same reason `operator` and `password` are
    withheld."""
    tools = {t["name"]: t for t in api.get(
        "/capabilities", headers=_at(authority, "teller1", "WEST-014")).get_json()}
    schema = tools["meridian.member_lookup"]["input_schema"]
    assert "branch" not in (schema.get("properties") or {})
    assert "member_id" in (schema.get("properties") or {})


# ---------------------------------------------------------------------------
# The claim is re-validated, which is what makes carrying it safe
# ---------------------------------------------------------------------------
def test_a_branch_this_deployment_does_not_list_is_refused(api, authority):
    """The load-bearing test. A correctly signed token -- what a leaked key
    buys -- still cannot put an arbitrary string in the audit trail or send one
    to the host."""
    r = _lookup(api, _at(authority, "teller1", "ELSEWHERE-999"))
    assert r.status_code == 403
    refusal = r.get_json()["refusal"]
    assert refusal["code"] == "BRANCH_NOT_CONFIGURED"
    assert "ELSEWHERE-999" in refusal["reason"]


def test_nothing_is_opened_when_the_branch_is_refused(api, authority, seen):
    """Refused before a browser is launched, like every other guardrail here."""
    seen.clear()
    _lookup(api, _at(authority, "teller1", "ELSEWHERE-999"))
    assert seen == {}, "the engine must not have been reached"


@pytest.mark.parametrize("api", [[]], indirect=True)
def test_a_deployment_that_configures_no_branches_refuses_a_claimed_one(
        api, authority):
    """Fail closed. If no choice was ever on offer, a token claiming one is not
    describing something that happened, so it is refused rather than passed
    through as a label nobody can account for."""
    r = _lookup(api, _at(authority, "teller1", "MAIN-001"))
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] == "BRANCH_NOT_CONFIGURED"


@pytest.mark.parametrize("api", [[]], indirect=True)
def test_with_no_branches_configured_a_plain_session_still_works(
        api, authority, seen):
    """...and falls back to the operator's own configured branch, which is how
    this behaved before sign-in existed."""
    _lookup(api, {"X-Bankcua-Session":
                  authority.mint(authority.store.get("teller1"))})
    assert seen["branch"] == "MAIN-001"


def test_the_direct_agent_path_is_untouched(api, seen):
    """Nobody signed in, so nobody chose. The operator's configured branch
    stands, and every CLI demo in the README keeps working."""
    api.post("/invoke/meridian.member_lookup",
             json={"operator": "teller1", "params": {"member_id": "100234"}})
    assert seen["branch"] == "MAIN-001"


def test_the_api_publishes_the_branches_it_will_accept(api):
    """The console builds its dropdown from this, so it cannot offer a choice
    that /invoke would refuse."""
    assert api.get("/branches").get_json() == BRANCHES


def test_the_configured_branches_are_the_targets_own():
    """The shipped config must match MERIDIAN's sign-on dropdown exactly.

    This is the test that would have caught inventing plausible-looking codes:
    `branch` is a value typed into the host's own form, so a code it does not
    offer is not a configuration preference, it is a broken run. If the target
    adds a branch, this list and this test move together.
    """
    from bankcua.service import ServiceConfig

    shipped = ServiceConfig.from_yaml(os.path.join(ROOT, "config",
                                                   "service.yaml"))
    assert shipped.branches == BRANCHES
    assert shipped.branch_codes == CODES


def test_a_bare_code_is_accepted_as_well_as_a_named_one():
    """The display name is a convenience; a deployment that wants none should
    not have to write a mapping to say so."""
    from bankcua.service import _branches

    assert _branches(["MAIN-001"]) == [{"code": "MAIN-001", "name": ""}]
    assert _branches([{"code": "X-1", "name": "Somewhere"}]) == [
        {"code": "X-1", "name": "Somewhere"}]
    # An entry with no code would be an option nobody could choose meaningfully.
    assert _branches([{"name": "nameless"}, "", None]) == []


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------
@pytest.fixture
def portal(tmp_path, authority):
    """The console, told about the same branches the API validates against."""
    from bankcua.portal.app import create_app as create_portal
    _skip_without_catalog()
    return create_portal(
        catalog_dir=CATALOG, evidence_dirs=(str(tmp_path / "ev"),),
        handoff_dir=str(tmp_path / "handoffs"),
        principals_path=str(authority.store.path),
        session_key_path=str(tmp_path / "session.key"),
        branches=BRANCHES, session_authority=authority).test_client()


def test_the_branch_dropdown_sits_below_the_operator(portal):
    page = portal.get("/login").get_data(as_text=True)
    assert 'id="branch"' in page
    for branch in BRANCHES:
        assert f'value="{branch["code"]}"' in page
        # Shown the way MERIDIAN's own sign-on screen shows it, so a person
        # picking a branch here is picking the row they would have picked there.
        assert f'{branch["code"]} - {branch["name"]}' in page
    assert page.index("Operator") < page.index(">Branch<"), "operator first"


def test_signing_in_at_a_branch_puts_it_on_the_session(portal, authority):
    r = portal.post("/login", data={"username": "teller1",
                                    "branch": "EAST-022"})
    assert r.status_code == 302
    me = portal.get("/me").get_json()
    assert me["branch"] == "EAST-022" and me["username"] == "teller1"
    # ...and it is on screen, because a person needs to see which branch they
    # are working as before they post a transfer from it.
    assert "EAST-022" in portal.get("/").get_data(as_text=True)


def test_a_branch_the_deployment_does_not_list_is_refused_at_the_door(portal):
    """Checked here as well as at the API. The API's refusal is the one that
    matters; this one exists so a person gets a sentence rather than a session
    that fails on its first request."""
    r = portal.post("/login", data={"username": "teller1",
                                    "branch": "ELSEWHERE-999"})
    assert r.status_code == 400
    assert b"not a branch this deployment recognises" in r.data
    assert portal.get("/me").status_code == 401, "no session was issued"


def test_a_form_posted_without_the_field_takes_the_first_branch(portal):
    """An old bookmark or a script posting just a username must not produce a
    session that records no branch on everything it then does."""
    assert portal.post("/login", data={"username": "teller1"}).status_code == 302
    assert portal.get("/me").get_json()["branch"] == CODES[0]


def test_the_console_offers_no_choice_when_none_is_configured(tmp_path,
                                                              authority):
    """A deployment with no `branches` list keeps the pre-existing behaviour:
    no dropdown, and every run falls back to the operator's own branch."""
    from bankcua.portal.app import create_app as create_portal
    _skip_without_catalog()
    client = create_portal(
        catalog_dir=CATALOG, evidence_dirs=(str(tmp_path / "ev"),),
        handoff_dir=str(tmp_path / "handoffs"),
        principals_path=str(authority.store.path),
        session_key_path=str(tmp_path / "session.key"),
        branches=[], session_authority=authority).test_client()
    assert 'id="branch"' not in client.get("/login").get_data(as_text=True)
    assert client.post("/login", data={"username": "teller1"}).status_code == 302
    assert client.get("/me").get_json()["branch"] == ""


def test_the_console_reads_its_branches_from_the_service_config(tmp_path):
    """One file, two readers. The list a person is offered and the list the API
    enforces cannot drift apart, because there is only one of them."""
    from bankcua.portal.app import configured_branches
    cfg = tmp_path / "service.yaml"
    cfg.write_text("branches:\n  - code: AAA-001\n    name: Alpha\n  - BBB-002\n")
    assert configured_branches(str(cfg)) == [
        {"code": "AAA-001", "name": "Alpha"}, {"code": "BBB-002", "name": ""}]
    # A missing or malformed config stops the CHOICE being offered; it must not
    # stop people signing in.
    assert configured_branches(str(tmp_path / "nope.yaml")) == []
    bad = tmp_path / "bad.yaml"
    bad.write_text("branches: [unclosed\n")
    assert configured_branches(str(bad)) == []
