"""
Value-level policy wired end to end through the replay engine.

The point of these: the value layer and the step layer are INDEPENDENT guardrails
that compose. One gates the amount before anything opens; the other gates the
irreversible click. Neither substitutes for the other.
"""
import os

import pytest

pytest.importorskip("playwright")

from bankcua.schema import CapabilityArtifact
from bankcua.replay.engine import ReplayEngine
from bankcua.replay.result import ReplayStatus
from bankcua.observability.logging import RunLogger
from bankcua.safety.policy import Policy, PolicyEngine, ValueRule
from bankcua.surface.web_playwright import WebSurface

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
ART = os.path.join(ROOT, "capabilities", "corebank.open_subaccount.json")
CREDS = {"username": "operator", "password": "password123"}
RULES = {"deposit": ValueRule(max=10_000.0, dual_control_above=1_000.0, unit=" USD")}


def _load(mock_app):
    if not os.path.exists(ART):
        pytest.skip("artifact missing")
    a = CapabilityArtifact.from_json(open(ART).read())
    a.target.base_url = mock_app
    return a


def _replay(art, params, tmp_path, name, **kw):
    logger = RunLogger(str(tmp_path / name), "replay", art.secret_params(),
                       {n: str(params[n]) for n in art.secret_params()})
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"],
                             value_rules=RULES),
                      allow_risky_override=True)
    surf = WebSurface(art.target.base_url, headless=True)
    surf.start()
    try:
        return ReplayEngine(surf, pe, logger, None, **kw).run(art, params)
    finally:
        surf.stop()


def _params(deposit):
    return {**CREDS, "member_id": "12345", "acct_type": "Money Market",
            "deposit": deposit}


def test_ceiling_breach_fails_before_the_browser_navigates(mock_app, tmp_path):
    art = _load(mock_app)
    r = _replay(art, _params("25000.00"), tmp_path, "ceiling")
    assert r.status == ReplayStatus.FAILURE
    assert r.failure.code == "VALUE_LIMIT_EXCEEDED"
    assert r.steps_executed == 0          # nothing was opened, nothing was typed


def test_dual_control_unmet_fails_closed_when_unattended(mock_app, tmp_path):
    art = _load(mock_app)
    r = _replay(art, _params("1500.00"), tmp_path, "unmet")
    assert r.status == ReplayStatus.FAILURE
    assert r.failure.code == "DUAL_CONTROL_REQUIRED"
    assert r.steps_executed == 0


def test_a_run_cannot_approve_itself(mock_app, tmp_path):
    art = _load(mock_app)
    r = _replay(art, _params("1500.00"), tmp_path, "self",
                initiator="alice", approver="alice")
    assert r.failure.code == "DUAL_CONTROL_REQUIRED"


def test_independent_approver_lets_the_run_proceed(mock_app, tmp_path):
    """It proceeds past the VALUE gate and is then stopped by the independent
    STEP gate on the irreversible click -- two layers, both doing their job."""
    art = _load(mock_app)
    r = _replay(art, _params("1500.00"), tmp_path, "countersigned",
                initiator="alice", approver="bruce")
    assert r.failure.code == "CONFIRMATION_REQUIRED"      # not DUAL_CONTROL
    assert r.steps_executed > 0

    log = (tmp_path / "countersigned" / "run.jsonl").read_text()
    assert "dual_control_satisfied" in log


def test_below_threshold_needs_no_countersignature(mock_app, tmp_path):
    art = _load(mock_app)
    r = _replay(art, _params("500.00"), tmp_path, "under")
    assert r.failure.code == "CONFIRMATION_REQUIRED"
    log = (tmp_path / "under" / "run.jsonl").read_text()
    assert "dual_control" not in log
