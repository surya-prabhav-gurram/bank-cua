"""
Replay engine edge cases beyond the happy/business/timeout paths:
validation-error outcome, app-error detection, locator drift telemetry,
missing-parameter guard, and the value-attribute extraction fallback.
"""
import os
import urllib.request

import pytest

pytest.importorskip("playwright")

from bankcua.schema import (CapabilityArtifact, LocatorCandidate, LocatorKind,
                            Step, ActionType, Extraction, Locator, Checkpoint,
                            Target, InputParameter)
from bankcua.replay.engine import ReplayEngine
from bankcua.replay.result import ReplayStatus
from bankcua.observability.logging import RunLogger
from bankcua.safety.policy import Policy, PolicyEngine
from bankcua.surface.web_playwright import WebSurface

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
ART = os.path.join(ROOT, "capabilities", "corebank.member_savings_lookup.json")
CREDS = {"username": "operator", "password": "password123"}


def _load(mock_app):
    if not os.path.exists(ART):
        pytest.skip("artifact missing")
    a = CapabilityArtifact.from_json(open(ART).read())
    a.target.base_url = mock_app
    return a


def _replay(mock_app, tmp_path, art, params, allow_risky=False, name="r"):
    logger = RunLogger(str(tmp_path / name), "replay", art.secret_params(),
                       [str(params.get(n)) for n in art.secret_params()])
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"]),
                      allow_risky_override=allow_risky)
    surf = WebSurface(art.target.base_url, headless=True)
    surf.start()
    try:
        return ReplayEngine(surf, pe, logger, None).run(art, params)
    finally:
        surf.stop()


def _reset(mock):
    urllib.request.urlopen(f"{mock}/_control/reset").read()


def test_validation_error_is_business_outcome(mock_app, tmp_path):
    _reset(mock_app)
    art = _load(mock_app)
    # empty member_id -> /member?mid= -> app returns a validation error
    r = _replay(mock_app, tmp_path, art, {**CREDS, "member_id": ""})
    assert r.status == ReplayStatus.BUSINESS_OUTCOME
    assert r.business_outcome.code == "VALIDATION_ERROR"


def test_app_error_500_detected(mock_app, tmp_path):
    _reset(mock_app)
    urllib.request.urlopen(f"{mock_app}/_control/set?key=inject&value=error500").read()
    try:
        art = _load(mock_app)
        r = _replay(mock_app, tmp_path, art, {**CREDS, "member_id": "12345"})
    finally:
        _reset(mock_app)
    # the 500 is a recoverable condition; it persists here so recovery is
    # exhausted -> a clean, coded hard failure (never a silent proceed)
    assert r.status == ReplayStatus.FAILURE
    assert "APP_ERROR_500" in r.failure.code


def test_locator_drift_signal(mock_app, tmp_path):
    _reset(mock_app)
    art = _load(mock_app)
    # prepend a bogus primary candidate to the Sign On click; replay must fall
    # back to a working candidate AND record the drift
    tgt = art.steps[2].target
    tgt.candidates.insert(0, LocatorCandidate(kind=LocatorKind.CSS,
                                              value="button#nope-not-here"))
    r = _replay(mock_app, tmp_path, art, {**CREDS, "member_id": "12345"})
    assert r.status == ReplayStatus.SUCCESS
    assert any(d.step_index == 2 and d.candidate_index >= 1 for d in r.drifts)


def test_missing_required_param_raises(mock_app, tmp_path):
    art = _load(mock_app)
    with pytest.raises(ValueError):
        _replay(mock_app, tmp_path, art, {"username": "operator"})  # no member_id/pw


def test_replay_unrecoverable_condition_escalates(mock_app, tmp_path):
    """Brief 3.6: a replay that hits a failure it cannot recover from routes a
    human intervention (on the same live session) instead of just failing. Here
    the operator reviews but cannot salvage -> the run resolves as ESCALATED,
    carrying the intervention id. Off by default; enabled via
    escalate_unrecoverable + a coordinator so unattended runs still fail fast."""
    import threading
    import time

    from bankcua.escalation.handoff import (HandoffStore, HandoffCoordinator,
                                            InterventionStatus)
    _reset(mock_app)
    # a top-level success checkpoint that can never hold -> unrecoverable failure
    art = CapabilityArtifact(
        id="probe-unrec", name="p", description="d",
        target=Target(app_id="a", base_url=mock_app, entry_path="/login"),
        inputs=[InputParameter(name="username", sensitive=True),
                InputParameter(name="password", sensitive=True),
                InputParameter(name="member_id")],
        outputs=[],
        success=Checkpoint(kind="text_present", value="NO_SUCH_TEXT_ZZZ"),
        steps=[Step(index=0, intent="land", action=ActionType.NAVIGATE,
                    url_template="/login")])

    store = HandoffStore(str(tmp_path / "handoffs"))
    logger = RunLogger(str(tmp_path / "run"), "replay", art.secret_params(),
                       ["operator", "password123"])
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"]))
    surf = WebSurface(art.target.base_url, headless=True)
    surf.start()
    coord = HandoffCoordinator(store, logger)

    def operator():
        # attend the console: resolve the first open intervention (control back
        # to the agent) but with no salvage -- the checkpoint still won't hold.
        for _ in range(400):
            openreqs = store.list_open()
            if openreqs:
                r = openreqs[0]
                r.status = InterventionStatus.RESOLVED
                r.controller = "agent"
                r.resume = True
                r.resolution_note = "reviewed; cannot salvage"
                store.write(r)
                return
            time.sleep(0.05)

    t = threading.Thread(target=operator)
    t.start()
    try:
        res = ReplayEngine(surf, pe, logger, coord,
                           escalate_unrecoverable=True).run(
            art, {**CREDS, "member_id": "12345"})
    finally:
        t.join(timeout=8)
        surf.stop()

    assert res.status == ReplayStatus.ESCALATED
    assert res.intervention_id and "unrecoverable" in res.intervention_id


def test_replay_unrecoverable_no_escalation_when_disabled(mock_app, tmp_path):
    """Default (no escalate_unrecoverable / no coordinator): the same
    unrecoverable failure fails fast with a coded FailureDetail, never blocking
    on a human. This is the unattended contract."""
    _reset(mock_app)
    art = CapabilityArtifact(
        id="probe-unrec2", name="p", description="d",
        target=Target(app_id="a", base_url=mock_app, entry_path="/login"),
        inputs=[InputParameter(name="username", sensitive=True),
                InputParameter(name="password", sensitive=True),
                InputParameter(name="member_id")],
        outputs=[],
        success=Checkpoint(kind="text_present", value="NO_SUCH_TEXT_ZZZ"),
        steps=[Step(index=0, intent="land", action=ActionType.NAVIGATE,
                    url_template="/login")])
    r = _replay(mock_app, tmp_path, art, {**CREDS, "member_id": "12345"})
    assert r.status == ReplayStatus.FAILURE
    assert r.failure.code == "SUCCESS_CHECKPOINT_FAILED"


def test_value_attribute_extraction_falls_back_to_text(mock_app, tmp_path):
    """A read with attribute='value' on a non-input cell must fall back to text."""
    _reset(mock_app)
    art = CapabilityArtifact(
        id="probe", name="p", description="d",
        target=Target(app_id="a", base_url=mock_app, entry_path="/login"),
        inputs=[InputParameter(name="username", sensitive=True),
                InputParameter(name="password", sensitive=True),
                InputParameter(name="member_id")],
        outputs=[], success=Checkpoint(kind="text_present", value="Savings",
                                       frame_path=["balancepane"]),
        steps=[
            Step(index=0, intent="user", action=ActionType.FILL,
                 target=Locator(description="user", candidates=[LocatorCandidate(
                     kind=LocatorKind.XPATH,
                     value='//tr[td[normalize-space(.)="User ID"]]//input')]),
                 value={"kind": "secret_param", "param": "username"}),
            Step(index=1, intent="pw", action=ActionType.FILL,
                 target=Locator(description="pw", candidates=[LocatorCandidate(
                     kind=LocatorKind.XPATH,
                     value='//tr[td[normalize-space(.)="Password"]]//input')]),
                 value={"kind": "secret_param", "param": "password"}),
            Step(index=2, intent="signon", action=ActionType.CLICK,
                 target=Locator(description="signon", candidates=[LocatorCandidate(
                     kind=LocatorKind.ROLE, role="button", value="Sign On")])),
            Step(index=3, intent="go", action=ActionType.NAVIGATE,
                 url_template="/member?mid={member_id}"),
            Step(index=4, intent="read name via value attr", action=ActionType.EXTRACT,
                 extract=Extraction(output="member_name", attribute="value",
                     locator=Locator(description="name cell", candidates=[
                         LocatorCandidate(kind=LocatorKind.XPATH,
                             value='//tr[td[normalize-space(.)="Name"]]/td[last()]')]))),
        ])
    r = _replay(mock_app, tmp_path, art, {**CREDS, "member_id": "12345"})
    assert r.status == ReplayStatus.SUCCESS
    assert r.outputs["member_name"] == "Jane A. Doe"
