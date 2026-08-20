"""
Two blind spots in the original replay contract, closed:

  * a FILL's effect is on the CONTROL, not the page, so no page-state checkpoint
    can see a write that silently failed;
  * a legitimate non-success can still carry declared data the caller needs.

Both are exercised against the live mock, because both are about what the
browser actually did rather than what the code intended.
"""
import os
import urllib.request

import pytest

pytest.importorskip("playwright")

from bankcua.schema import CapabilityArtifact
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


def _replay(art, params, tmp_path, name):
    logger = RunLogger(str(tmp_path / name), "replay", art.secret_params(),
                       {n: str(params[n]) for n in art.secret_params()})
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"]))
    surf = WebSurface(art.target.base_url, headless=True)
    surf.start()
    try:
        return ReplayEngine(surf, pe, logger, None).run(art, params)
    finally:
        surf.stop()


def _ctl(mock, q):
    urllib.request.urlopen(f"{mock}/_control/{q}").read()


# ---- Step.verify_value ----------------------------------------------------
def test_silently_swallowed_fill_is_caught(mock_app, tmp_path):
    """The injected field ACCEPTS the keystrokes and discards them: the action
    reports success and the page looks normal, so only the read-back sees it."""
    _ctl(mock_app, "set?key=inject&value=swallow")
    try:
        art = _load(mock_app)
        r = _replay(art, {**CREDS, "member_id": "12345"}, tmp_path, "swallow")
    finally:
        _ctl(mock_app, "reset")

    assert r.status == ReplayStatus.FAILURE
    assert r.failure.code == "FILL_NOT_APPLIED"
    assert r.failure.step_index == 3
    assert "12345" in r.failure.expected
    assert r.failure.evidence.get("screenshot") and r.failure.evidence.get("dom")


def test_secret_fill_is_verified_without_comparing_the_value(mock_app, tmp_path):
    """A credential is asserted non-empty only. Reading one back to diff it would
    reintroduce exactly the leak the observation indexer avoids."""
    art = _load(mock_app)
    pw_step = next(s for s in art.steps
                   if s.value is not None and s.value.kind == "secret_param"
                   and s.value.param == "password")
    assert pw_step.verify_value is True

    r = _replay(art, {**CREDS, "member_id": "12345"}, tmp_path, "secretfill")
    assert r.status == ReplayStatus.SUCCESS

    log = (tmp_path / "secretfill" / "run.jsonl").read_text()
    assert "password123" not in log


# ---- KnownCondition.surfaces_outputs -------------------------------------
def test_permission_denied_surfaces_the_data_it_has(mock_app, tmp_path):
    """Corebank's denial screen withholds the balance but still identifies the
    member. The caller needs that to route the request."""
    art = _load(mock_app)
    r = _replay(art, {**CREDS, "member_id": "99999"}, tmp_path, "denied")

    assert r.status == ReplayStatus.BUSINESS_OUTCOME
    assert r.business_outcome.code == "PERMISSION_DENIED"
    assert r.business_outcome.outputs_surfaced == ["member_name"]
    assert r.outputs["member_name"] == "Restricted Account"
    # the withheld value stays withheld -- surfacing is best-effort, not a bypass
    assert "savings_balance" not in r.outputs


def test_not_found_surfaces_nothing(mock_app, tmp_path):
    """Control case: whether an outcome carries data is a property of the vendor's
    UI, declared per condition -- not something guessed at runtime."""
    art = _load(mock_app)
    r = _replay(art, {**CREDS, "member_id": "00000"}, tmp_path, "missing")

    assert r.status == ReplayStatus.BUSINESS_OUTCOME
    assert r.business_outcome.code == "MEMBER_NOT_FOUND"
    assert r.business_outcome.outputs_surfaced == []
    assert r.outputs == {}


def test_surfacing_never_turns_an_outcome_into_a_success(mock_app, tmp_path):
    """Partial data does not upgrade the verdict: the caller must still branch on
    the business code."""
    art = _load(mock_app)
    r = _replay(art, {**CREDS, "member_id": "99999"}, tmp_path, "verdict")
    assert r.status != ReplayStatus.SUCCESS
    assert r.ok() is False
