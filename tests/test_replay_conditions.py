"""
Replay engine edge cases beyond the happy/business/timeout paths:
validation-error outcome, app-error detection, locator drift telemetry,
missing-parameter guard, and the value-attribute extraction fallback.
"""
import os
import urllib.request

import pytest

pytest.importorskip("playwright")

from bankcua.schema import (CapabilityArtifact, LocatorCandidate, LocatorKind,  # noqa
                            Step, ActionType, Extraction, Locator, Checkpoint,
                            Target, InputParameter)
from bankcua.replay.engine import ReplayEngine            # noqa: E402
from bankcua.replay.result import ReplayStatus            # noqa: E402
from bankcua.observability.logging import RunLogger       # noqa: E402
from bankcua.safety.policy import Policy, PolicyEngine    # noqa: E402
from bankcua.surface.web_playwright import WebSurface     # noqa: E402

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
