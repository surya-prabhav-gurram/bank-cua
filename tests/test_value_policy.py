"""
Value-level (semantic) policy: the layer that can tell $1 from $1M.

The URL/action allowlist answers "may the agent click here". It cannot answer
"is this amount sane", so these rules inspect the *inputs* a capability is
invoked with, before the browser opens.
"""
import pytest

from bankcua.safety.policy import (Decision, Policy, PolicyEngine,
                                   PolicyViolation, ValueRule)

RULES = {"deposit": ValueRule(max=10_000.0, dual_control_above=1_000.0, unit=" USD")}


def _engine():
    return PolicyEngine(Policy(value_rules=RULES))


def test_value_under_all_thresholds_is_allowed():
    assert _engine().evaluate_inputs({"deposit": "500.00"}).decision == Decision.ALLOW


def test_hard_ceiling_is_refused_not_escalated():
    """A ceiling breach is never routed to a human: no operator at a browser
    should be able to wave through an amount the institution has ruled out."""
    with pytest.raises(PolicyViolation) as ex:
        _engine().evaluate_inputs({"deposit": "25000"})
    assert "exceeds the permitted maximum" in str(ex.value)


def test_above_threshold_requires_dual_control_and_names_the_param():
    d = _engine().evaluate_inputs({"deposit": "1500.00"})
    assert d.decision == Decision.NEEDS_CONFIRMATION
    assert d.params == ("deposit",)
    assert "deposit" in d.reason


def test_unparseable_governed_value_is_refused():
    """A limit you cannot evaluate is not a limit -- fail closed rather than
    letting an unreadable value through."""
    with pytest.raises(PolicyViolation):
        _engine().evaluate_inputs({"deposit": "five hundred"})


def test_money_formatting_is_tolerated():
    with pytest.raises(PolicyViolation):
        _engine().evaluate_inputs({"deposit": "$25,000.00"})
    assert _engine().evaluate_inputs({"deposit": "$500.00"}).decision == Decision.ALLOW


def test_ungoverned_parameters_are_ignored():
    assert _engine().evaluate_inputs({"member_id": "999999999"}).decision == Decision.ALLOW


def test_dual_control_needs_two_different_people():
    pe = _engine()
    assert pe.approver_is_independent("bruce", "alice") is True
    assert pe.approver_is_independent("alice", "alice") is False
    assert pe.approver_is_independent("  Alice ", "alice") is False   # not a loophole
    assert pe.approver_is_independent("", "alice") is False


def test_rules_load_from_yaml(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text("value_rules:\n  deposit:\n    max: 10.0\n    dual_control_above: 5.0\n")
    pol = Policy.from_yaml(str(p))
    assert pol.value_rules["deposit"].max == 10.0
    assert pol.value_rules["deposit"].dual_control_above == 5.0
