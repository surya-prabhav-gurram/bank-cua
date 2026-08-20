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


def test_ceiling_breach_is_refused_before_the_browser_navigates(mock_app, tmp_path):
    """A ceiling breach is a DECISION, not a fault: nothing broke, the request was
    declined. The caller's move is a smaller amount, not an incident."""
    art = _load(mock_app)
    r = _replay(art, _params("25000.00"), tmp_path, "ceiling")
    assert r.status == ReplayStatus.REFUSED
    assert r.failure is None               # never typed as a system failure
    assert r.refusal.code == "VALUE_LIMIT_EXCEEDED"
    assert "10000" in r.refusal.reason
    assert r.steps_executed == 0          # nothing was opened, nothing was typed


def test_dual_control_unmet_fails_closed_when_unattended(mock_app, tmp_path):
    art = _load(mock_app)
    r = _replay(art, _params("1500.00"), tmp_path, "unmet")
    assert r.status == ReplayStatus.REFUSED
    assert r.refusal.code == "DUAL_CONTROL_REQUIRED"
    assert "approver" in r.refusal.requirement
    assert r.steps_executed == 0


def test_a_run_cannot_approve_itself(mock_app, tmp_path):
    art = _load(mock_app)
    r = _replay(art, _params("1500.00"), tmp_path, "self",
                initiator="alice", approver="alice")
    assert r.status == ReplayStatus.REFUSED
    assert r.refusal.code == "DUAL_CONTROL_REQUIRED"


def test_independent_approver_lets_the_run_proceed(mock_app, tmp_path):
    """It proceeds past the VALUE gate and is then stopped by the independent
    STEP gate on the irreversible click -- two layers, both doing their job."""
    art = _load(mock_app)
    r = _replay(art, _params("1500.00"), tmp_path, "countersigned",
                initiator="alice", approver="bruce")
    assert r.status == ReplayStatus.REFUSED
    assert r.refusal.code == "CONFIRMATION_REQUIRED"      # not DUAL_CONTROL
    assert r.steps_executed > 0

    log = (tmp_path / "countersigned" / "run.jsonl").read_text()
    assert "dual_control_satisfied" in log


def test_below_threshold_needs_no_countersignature(mock_app, tmp_path):
    art = _load(mock_app)
    r = _replay(art, _params("500.00"), tmp_path, "under")
    assert r.status == ReplayStatus.REFUSED
    assert r.refusal.code == "CONFIRMATION_REQUIRED"
    log = (tmp_path / "under" / "run.jsonl").read_text()
    assert "dual_control" not in log


def test_a_refusal_is_never_typed_as_a_failure(mock_app, tmp_path):
    """The distinction this contract exists to make. A caller branching on
    `failure` must not be told a policy decision broke something -- that is the
    same conflation, one layer up, as calling "no such member" a crash."""
    art = _load(mock_app)
    refused = _replay(art, _params("25000.00"), tmp_path, "refused")
    assert refused.status == ReplayStatus.REFUSED
    assert refused.refusal is not None and refused.failure is None
    assert refused.ok() is False        # still not a success

    # and a genuine fault is still a fault, with a debuggable failure attached
    broken = _load(mock_app)
    broken.target.base_url = "http://127.0.0.1:5999"
    hard = _replay(broken, _params("500.00"), tmp_path, "hard")
    assert hard.status == ReplayStatus.FAILURE
    assert hard.failure is not None and hard.refusal is None


# ---------------------------------------------------------------------------
# Velocity: the limit a per-invocation ceiling cannot see.
# ---------------------------------------------------------------------------
VELOCITY_RULES = {"deposit": ValueRule(max=10_000.0, max_per_window=5_000.0,
                                       window_seconds=3600, unit=" USD")}


def _velocity_replay(art, params, tmp_path, name, ledger):
    """As _replay, but with a rolling-window rule and a real ledger attached."""
    logger = RunLogger(str(tmp_path / name), "replay", art.secret_params(),
                       {n: str(params[n]) for n in art.secret_params()})
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"],
                             value_rules=VELOCITY_RULES),
                      allow_risky_override=True)
    surf = WebSurface(art.target.base_url, headless=True)
    surf.start()
    try:
        return ReplayEngine(surf, pe, logger, None, ledger=ledger).run(art, params)
    finally:
        surf.stop()


def test_velocity_ceiling_refuses_once_the_window_total_would_be_exceeded(
        mock_app, tmp_path):
    """Ten $999 deposits clear a $1,000 per-invocation limit ten times over.

    Each amount here is individually legal against `max`; only the trailing sum
    is not. A limit that cannot see the sum is the gap this rule closes, so the
    refusal has to come from history rather than from the request alone.
    """
    from bankcua.safety.ledger import Ledger, LedgerEntry
    art = _load(mock_app)
    ledger = Ledger(str(tmp_path / "value_ledger.jsonl"))
    # $4,500 already spent in the window, by an earlier run of a DIFFERENT
    # capability -- the budget is the parameter's, not the flow's.
    ledger.record(LedgerEntry(ts=__import__("time").time(),
                              capability_id="corebank.some_other_flow",
                              param="deposit", value=4500.0))

    r = _velocity_replay(art, _params("900.00"), tmp_path, "velocity", ledger)
    assert r.status == ReplayStatus.REFUSED
    assert r.refusal.code == "VALUE_LIMIT_EXCEEDED"
    assert "5000" in r.refusal.reason and "4500" in r.refusal.reason
    assert r.steps_executed == 0


def test_a_refused_run_does_not_spend_the_velocity_budget(mock_app, tmp_path):
    """A run that moved no money must not be charged for it.

    Booking on attempt rather than on success would let a caller exhaust the
    window with requests the system already refused -- a guardrail that
    denial-of-services the thing it protects.
    """
    from bankcua.safety.ledger import Ledger
    art = _load(mock_app)
    ledger = Ledger(str(tmp_path / "value_ledger.jsonl"))

    # refused at the step gate (irreversible click, no operator) -> no money moved
    r = _velocity_replay(art, _params("900.00"), tmp_path, "nospend", ledger)
    assert r.status == ReplayStatus.REFUSED
    assert ledger.total_in_window("deposit", 3600) == 0.0


def test_ledger_scopes_the_window_by_parameter_across_capabilities(tmp_path):
    """Two flows that both move money share one budget: splitting it per flow is
    the gap an attacker walks through."""
    from bankcua.safety.ledger import Ledger, LedgerEntry
    now = 1_000_000.0
    ledger = Ledger(str(tmp_path / "l.jsonl"), clock=lambda: now)
    ledger.record(LedgerEntry(ts=now - 10, capability_id="flow.a",
                              param="deposit", value=300.0))
    ledger.record(LedgerEntry(ts=now - 20, capability_id="flow.b",
                              param="deposit", value=200.0))
    ledger.record(LedgerEntry(ts=now - 9_999, capability_id="flow.a",
                              param="deposit", value=999.0))   # outside window
    ledger.record(LedgerEntry(ts=now - 10, capability_id="flow.a",
                              param="transfer", value=50.0))   # other parameter
    assert ledger.total_in_window("deposit", 3600) == 500.0
    assert ledger.total_in_window("deposit", 3600, capability_id="flow.a") == 300.0
