"""
The capability API, and specifically the part that is easy to get wrong.

Wrapping capabilities in an HTTP surface is where a safety model usually dies:
the wrapper starts accepting the decisions the guardrails were supposed to make.
The earlier version of this service took `allow_risky` and `allow_unapproved`
from the REQUEST BODY and a password per call, which meant the caller chose
whether irreversible actions were permitted and which operator to be. With a
language model as the caller, that is a model authorising funds transfers.

Most of what follows asserts negatives -- that a crafted request cannot buy
itself privilege -- because those are the assertions that stop the wrapper from
quietly becoming the way around the guardrails.
"""
import json
import os

import pytest

pytest.importorskip("playwright")

from bankcua.safety.credentials import (CredentialError, OperatorIdentity,
                                        StaticCredentialStore)
from bankcua.service import ServiceConfig, create_app

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
CATALOG = os.path.join(ROOT, "capabilities", "meridian")

BRANCH = {"branch": "MAIN-001"}
STORE = StaticCredentialStore({
    "teller1": OperatorIdentity("teller1", "teller", {"password": "password"}, BRANCH),
    "super1": OperatorIdentity("super1", "supervisor", {"password": "password"}, BRANCH),
})


def _skip_without_catalog():
    if not os.path.isdir(CATALOG) or not os.listdir(CATALOG):
        pytest.skip("meridian capabilities not recorded; run scripts/record_meridian.py")


@pytest.fixture
def client(tmp_path):
    _skip_without_catalog()
    cfg = tmp_path / "service.yaml"
    cfg.write_text(json.dumps({
        "default_operator": "teller1",
        "capabilities": {
            "meridian.member_lookup": {"allow_unapproved": True},
            "meridian.transfer_funds": {"allow_risky": True, "allow_unapproved": True},
            "meridian.place_hold": {"requires_role": "supervisor",
                                    "allowed_operators": ["super1"],
                                    "allow_unapproved": True},
        }}))
    app = create_app(catalog_dir=CATALOG,
                     service_config_path=str(cfg),
                     evidence_dir=str(tmp_path / "ev"),
                     # Its own handoff inbox, and a short wait. Left at the
                     # defaults, a test that escalates writes into the repo's
                     # COMMITTED evidence/handoffs -- so the suite rewrites
                     # curated evidence, and any console left running can
                     # resolve a test's pause from a browser, which turns "the
                     # run escalated" into "the run succeeded" and fails a test
                     # about privilege escalation for reasons nobody can see.
                     handoff_dir=str(tmp_path / "handoffs"),
                     handoff_timeout_s=10,
                     credential_store=STORE)
    return app.test_client()


# ---------------------------------------------------------------------------
# Discovery surface
# ---------------------------------------------------------------------------
def test_manifest_is_callable_by_an_agent_that_never_sees_the_ui(client):
    caps = client.get("/capabilities").get_json()
    by_name = {c["name"]: c for c in caps}
    assert "meridian.member_lookup" in by_name
    tool = by_name["meridian.member_lookup"]
    assert "member_id" in tool["input_schema"]["properties"]
    assert "shares" in tool["returns"]
    # Nothing in the manifest describes the UI -- that is the whole point.
    assert "steps" not in tool


def test_operators_endpoint_exposes_aliases_but_never_secrets(client):
    body = client.get("/operators").get_json()
    assert {o["alias"] for o in body} == {"teller1", "super1"}
    assert {o["role"] for o in body} == {"teller", "supervisor"}
    assert "password" not in json.dumps(body).lower()


def test_unknown_capability_is_404(client):
    assert client.post("/invoke/nope", json={"params": {}}).status_code == 404


# ---------------------------------------------------------------------------
# The wrapper must not become a way around the guardrails
# ---------------------------------------------------------------------------
def test_a_request_cannot_grant_itself_permission_for_irreversible_actions(client):
    """`allow_risky` in the body must be inert.

    place_hold is configured WITHOUT allow_risky, so its irreversible step is
    gated. A caller asking for it anyway must change nothing: if this ever
    passes through, a chatbot can authorise a hold on a member's account by
    adding one key to a JSON body."""
    r = client.post("/invoke/meridian.place_hold",
                    json={"operator": "super1", "allow_risky": True,
                          "params": {"member_id": "100234",
                                     "share_id": "100234-S0070",
                                     "reason_code": "FRAUD", "notes": "test"}})
    body = r.get_json()
    assert body["status"] != "success", (
        "a request-supplied allow_risky was honoured; the wrapper is now a "
        "privilege-escalation path")


def test_a_request_cannot_grant_itself_permission_to_run_an_unapproved_capability(
        tmp_path):
    """The approval gate belongs to the deployment, not to the caller.

    Runs against a COPY of the catalog forced to `draft`, rather than the repo's
    own artifacts: whether someone has run scripts/approve_meridian.py is a
    property of a demo, and a test that flips with it is testing the workspace
    instead of the behaviour.
    """
    _skip_without_catalog()
    from bankcua.schema import ApprovalState, CapabilityArtifact

    catalog = tmp_path / "catalog"
    catalog.mkdir()
    for src in os.listdir(CATALOG):
        art = CapabilityArtifact.from_json(
            open(os.path.join(CATALOG, src)).read())
        art.approval_state = ApprovalState.DRAFT
        (catalog / src).write_text(art.to_json())

    cfg = tmp_path / "strict.yaml"
    cfg.write_text(json.dumps({"default_operator": "teller1", "capabilities": {}}))
    app = create_app(catalog_dir=str(catalog), service_config_path=str(cfg),
                     evidence_dir=str(tmp_path / "ev2"), credential_store=STORE)
    r = app.test_client().post("/invoke/meridian.member_lookup",
                               json={"allow_unapproved": True,
                                     "params": {"member_id": "100234"}})
    assert r.status_code == 409
    assert r.get_json()["refusal"]["code"] == "CAPABILITY_NOT_APPROVED"


def test_a_missing_required_input_refuses_with_the_contract_not_a_500(client):
    """The typed contract is the whole promise of this API, so it is enforced at
    the boundary rather than deep inside replay.

    `validate_inputs` raises, and an exception escaping the handler is a 500 with
    an HTML body -- the one shape a caller cannot branch on, from the surface
    whose entire job is to return a structured result. It also launched a browser
    for a call that could never have run, and left a run directory with an empty
    summary that the dashboard then reported as `unknown`.

    A missing argument is a caller error, so it refuses like any other guardrail.
    """
    r = client.post("/invoke/meridian.member_lookup",
                    json={"operator": "teller1", "params": {}})
    assert r.status_code == 400, r.get_data(as_text=True)[:200]
    body = r.get_json()
    assert body["status"] == "refused"
    assert body["refusal"]["code"] == "MISSING_REQUIRED_INPUT"
    # It has to name what was missing, or the caller cannot fix the request.
    assert "member_id" in body["refusal"]["reason"]
    # ...and it must not name what the caller was never asked for.
    assert "password" not in json.dumps(body).lower()


def test_a_teller_cannot_invoke_a_supervisor_only_capability(client):
    """The host enforces this too. Refusing here as well means we never drive a
    member's account up to a wall we already know is there -- and the boundary
    does not depend on the target remembering to enforce it."""
    r = client.post("/invoke/meridian.place_hold",
                    json={"operator": "teller1",
                          "params": {"member_id": "100234",
                                     "share_id": "100234-S0070",
                                     "reason_code": "FRAUD", "notes": "test"}})
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] in {"ROLE_NOT_PERMITTED",
                                               "OPERATOR_NOT_PERMITTED"}


def test_an_unknown_operator_is_refused_rather_than_defaulted(client):
    """Falling back to the default operator on an unknown alias would silently
    run as somebody else."""
    r = client.post("/invoke/meridian.member_lookup",
                    json={"operator": "ghost", "params": {"member_id": "100234"}})
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] == "UNKNOWN_OPERATOR"


def test_a_caller_supplied_password_cannot_override_the_resolved_secret(client,
                                                                       tmp_path):
    """Secrets are merged AFTER caller params, so a param of the same name is
    overwritten rather than honoured. Otherwise the credential store is
    decorative: anyone could just send the field it was meant to supply."""
    from bankcua.service import create_app as make
    captured = {}

    class _Recording(StaticCredentialStore):
        def resolve(self, alias):
            identity = super().resolve(alias)
            captured["alias"] = alias
            return identity

    store = _Recording({"teller1": OperatorIdentity(
        "teller1", "teller", {"password": "the-real-secret"}, BRANCH)})
    cfg = tmp_path / "svc.yaml"
    cfg.write_text(json.dumps({"default_operator": "teller1", "capabilities": {
        "meridian.member_lookup": {"allow_unapproved": True}}}))
    app = make(catalog_dir=CATALOG, service_config_path=str(cfg),
               evidence_dir=str(tmp_path / "ev3"), credential_store=store,
               base_url_override="http://127.0.0.1:1")
    app.test_client().post("/invoke/meridian.member_lookup",
                           json={"params": {"member_id": "100234",
                                            "password": "attacker-supplied"}})
    assert captured.get("alias") == "teller1"
    # The run itself fails (no target at that URL); what matters is that the
    # store was consulted rather than the body trusted.


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
def test_missing_service_config_fails_closed():
    """An absent config must not mean 'allow'. A deployment that forgets to ship
    service.yaml should be able to do nothing, not everything."""
    cfg = ServiceConfig.from_yaml("/nonexistent/service.yaml")
    rule = cfg.rule_for("anything")
    assert rule.allow_risky is False and rule.allow_unapproved is False
    assert cfg.default_operator == ""


def test_unconfigured_capability_inherits_the_closed_default():
    cfg = ServiceConfig.from_yaml(os.path.join(ROOT, "config", "service.yaml"))
    rule = cfg.rule_for("meridian.not_configured_here")
    assert rule.allow_risky is False and rule.allow_unapproved is False


def test_credential_store_refuses_unknown_aliases():
    with pytest.raises(CredentialError):
        STORE.resolve("nobody")


# ---------------------------------------------------------------------------
# The manifest is the contract an agent reads. What it OMITS is load-bearing.
# ---------------------------------------------------------------------------
def test_the_manifest_never_advertises_a_credential(client):
    """A public tool list saying "this capability takes a password" is an
    invitation to send one.

    This is not hypothetical: given the unfiltered manifest, a live model stopped
    and asked the user for the operator's password instead of calling the tool --
    the manifest working exactly as written and exactly wrong. No caller should
    ever hold a credential; the service resolves one from an operator alias.
    """
    caps = client.get("/capabilities").get_json()
    blob = json.dumps(caps).lower()
    assert "password" not in blob
    for tool in caps:
        assert "password" not in tool["input_schema"]["properties"]
        assert "password" not in (tool["input_schema"].get("required") or [])


def test_the_manifest_omits_what_the_service_supplies(client):
    """A caller cannot know the branch code and must not choose the operator.
    Asking it for either produces a question it has no basis to answer."""
    caps = client.get("/capabilities").get_json()
    for tool in caps:
        props = set(tool["input_schema"]["properties"])
        assert not (props & {"operator", "branch"}), (
            f"{tool['name']} asks the caller for identity the service supplies")


def test_the_manifest_still_asks_for_what_the_caller_must_decide(client):
    """Filtering must not hollow out the contract."""
    caps = {c["name"]: c for c in client.get("/capabilities").get_json()}
    assert caps["meridian.member_lookup"]["input_schema"]["required"] == ["member_id"]
    transfer = set(caps["meridian.transfer_funds"]["input_schema"]["required"])
    assert {"member_id", "from_share", "to_share", "amount"} <= transfer
