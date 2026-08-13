"""Edge cases across transforms, the condition library, redaction/logging,
value sources, and the discovery-time policy guard."""

import pytest

from bankcua.replay.errors import apply_transform
from bankcua.knowledge import conditions_for, COREBANK_CONDITIONS
from bankcua.observability.logging import RunLogger
from bankcua.schema import ValueSource, ActionType, RiskClass, Step
from bankcua.safety.policy import (Policy, PolicyEngine, PolicyViolation,
                                   Decision)


# ---- transforms -------------------------------------------------------------
@pytest.mark.parametrize("val,tf,expected", [
    ("$4,213.55", "money_to_cents", 421355),
    ("$0.00", "money_to_cents", 0),
    ("n/a", "money_to_cents", 0),          # no digits -> 0
    ("  hi ", "strip", "hi"),
    ("SA-12345-8100", "digits_only", 123458100),
    ("abc", "digits_only", 0),
    ("keep", None, "keep"),                # no transform -> passthrough
    ("keep", "unknown", "keep"),           # unknown transform -> passthrough
])
def test_apply_transform(val, tf, expected):
    assert apply_transform(val, tf) == expected


def test_apply_transform_none_value():
    assert apply_transform(None, "strip") is None


# ---- vendor condition library ----------------------------------------------
def test_conditions_for_vendor():
    conds = conditions_for("Corebank")
    codes = {c.code for c in conds}
    assert codes == {c.code for c in COREBANK_CONDITIONS}
    assert conditions_for(None) == []
    assert conditions_for("UnknownVendor") == []


def test_conditions_for_returns_deep_copies():
    a = conditions_for("Corebank")
    a[0].message = "MUTATED"
    assert conditions_for("Corebank")[0].message != "MUTATED"   # library untouched


# ---- redaction inside the run logger ---------------------------------------
def test_run_logger_redacts_secrets_and_pii(tmp_path):
    logger = RunLogger(str(tmp_path), "replay", {"password"}, ["hunter2"])
    logger.event("step", password="hunter2", note="ssn 123-45-6789 hunter2",
                 member="12345")
    logger.finish({"password": "hunter2", "ok": True})
    text = (tmp_path / "run.jsonl").read_text() + (tmp_path / "summary.json").read_text()
    assert "hunter2" not in text                # secret literal gone everywhere
    assert "123-45-6789" not in text            # SSN pattern scrubbed
    assert "12345" in text                      # non-secret preserved


# ---- value sources ----------------------------------------------------------
def test_value_source_kinds():
    assert ValueSource(kind="literal", literal="x").resolve({}, {}) == "x"
    assert ValueSource(kind="param", param="m").resolve({"m": 7}, {}) == "7"
    assert ValueSource(kind="from_output", param="o").resolve({}, {"o": "v"}) == "v"
    with pytest.raises(KeyError):
        ValueSource(kind="param", param="missing").resolve({}, {})


# ---- discovery-time policy guard -------------------------------------------
def test_discovery_action_guard():
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"],
                             blocked_url_patterns=["*/logout*"]))
    # allowed safe action
    d = pe.evaluate_discovery_action(ActionType.CLICK, "http://127.0.0.1:5057/home",
                                     RiskClass.SAFE)
    assert d.decision == Decision.ALLOW
    # risky action without approval -> needs confirmation
    d2 = pe.evaluate_discovery_action(ActionType.CLICK, "http://127.0.0.1:5057/x",
                                      RiskClass.RISKY)
    assert d2.decision == Decision.NEEDS_CONFIRMATION
    # blocked URL -> raises
    with pytest.raises(PolicyViolation):
        pe.evaluate_discovery_action(ActionType.NAVIGATE,
                                     "http://127.0.0.1:5057/logout", RiskClass.SAFE)


def test_risky_step_allowed_with_override_but_confirmation_wins():
    # requires_confirmation takes precedence over --allow-risky (fail-safe)
    pe = PolicyEngine(Policy(), allow_risky_override=True)
    step = Step(index=0, intent="confirm", action=ActionType.CLICK,
                risk=RiskClass.RISKY, requires_confirmation=True)
    assert pe.evaluate_step(step).decision == Decision.NEEDS_CONFIRMATION
