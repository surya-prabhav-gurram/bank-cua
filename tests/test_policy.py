import pytest

from bankcua.safety.policy import Policy, PolicyEngine, PolicyViolation, Decision
from bankcua.schema import ActionType, RiskClass, Step


def _pol():
    return Policy(allowed_url_patterns=["http://127.0.0.1:*"],
                 blocked_url_patterns=["*/logout*"])


def test_allowlist_allows_and_blocks():
    pe = PolicyEngine(_pol())
    pe.check_url("http://127.0.0.1:5000/home")          # ok
    with pytest.raises(PolicyViolation):
        pe.check_url("http://evil.example.com/x")        # not allowed
    with pytest.raises(PolicyViolation):
        pe.check_url("http://127.0.0.1:5000/logout")     # explicitly blocked


def test_action_type_gate():
    pe = PolicyEngine(Policy(allowed_action_types={"navigate"}))
    with pytest.raises(PolicyViolation):
        pe.check_action_type(ActionType.CLICK)


def test_risky_step_blocked_without_approval():
    pe = PolicyEngine(_pol())  # allow_risky False
    step = Step(index=0, intent="confirm", action=ActionType.CLICK,
                risk=RiskClass.RISKY, requires_confirmation=False)
    d = pe.evaluate_step(step, "http://127.0.0.1:5000/x")
    assert d.decision == Decision.BLOCK


def test_risky_step_needs_confirmation_when_flagged():
    pe = PolicyEngine(_pol())
    step = Step(index=0, intent="confirm", action=ActionType.CLICK,
                risk=RiskClass.RISKY, requires_confirmation=True)
    d = pe.evaluate_step(step, "http://127.0.0.1:5000/x")
    assert d.decision == Decision.NEEDS_CONFIRMATION


def test_risky_allowed_with_override():
    pe = PolicyEngine(_pol(), allow_risky_override=True)
    step = Step(index=0, intent="confirm", action=ActionType.CLICK,
                risk=RiskClass.RISKY, requires_confirmation=False)
    d = pe.evaluate_step(step, "http://127.0.0.1:5000/x")
    assert d.decision == Decision.ALLOW


def test_artifact_allowlist_layered():
    pe = PolicyEngine(_pol(), artifact_url_patterns=["http://127.0.0.1:5000/*"])
    pe.check_url("http://127.0.0.1:5000/home")           # satisfies both
    with pytest.raises(PolicyViolation):
        pe.check_url("http://127.0.0.1:6000/home")        # global ok, artifact no
