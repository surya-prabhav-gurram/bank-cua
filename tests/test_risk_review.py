"""
Risk classification is a heuristic that PROPOSES; a human RATIFIES.

The first version classified risk by keyword alone. These tests pin the two
things that fixed: a structural signal the caption cannot fool, and an approval
gate that refuses to promote a capability while any risky step is unreviewed.
"""
import pytest

from bankcua.agent.loop import classify_risk
from bankcua.catalog import Catalog
from bankcua.schema import (ActionType, CapabilityArtifact, Checkpoint,
                            RiskClass, Step, Target)
from bankcua.surface.base import ElementInfo


def _el(**kw):
    return ElementInfo(ref=0, **kw)


# ---- classification -------------------------------------------------------
def test_lexical_signal_still_catches_captions():
    r, why = classify_risk(_el(name="Delete record"), "click")
    assert r == RiskClass.RISKY and "lexical" in why


def test_post_submit_corroborates_a_lexical_match():
    r, why = classify_risk(
        _el(name="Confirm and Create", is_submit=True, form_method="post"), "click")
    assert r == RiskClass.RISKY
    assert "lexical" in why and "corroborated" in why


def test_unlabelled_mutation_is_caught_by_the_structural_signal():
    """The gap the keyword regex could never close: a POST submit whose caption
    carries no risky word at all."""
    for caption in ("Apply Changes", "Proceed", "Finalise"):
        r, why = classify_risk(
            _el(name=caption, is_submit=True, form_method="post"), "click")
        assert r == RiskClass.RISKY, caption
        assert "structural" in why


def test_benign_submits_are_not_promoted():
    """A guardrail that cries wolf gets switched off. Authentication, queries and
    screen navigation POST too."""
    for caption in ("Sign On", "Log In", "Search", "Find", "Review", "Next"):
        r, _ = classify_risk(
            _el(name=caption, is_submit=True, form_method="post"), "click")
        assert r == RiskClass.SAFE, caption


def test_non_click_actions_are_never_risky():
    for action in ("navigate", "fill", "select", "press", "extract"):
        assert classify_risk(_el(name="Delete"), action)[0] == RiskClass.SAFE


# ---- the approval gate ----------------------------------------------------
def _artifact(risky_reviewed: bool) -> CapabilityArtifact:
    return CapabilityArtifact(
        id="t.cap", name="t", description="t",
        target=Target(app_id="a", base_url="http://127.0.0.1:1"),
        success=Checkpoint(kind="url_matches", value="/x"),
        steps=[
            Step(index=0, intent="safe nav", action=ActionType.NAVIGATE,
                 url_template="/x"),
            Step(index=1, intent="irreversible", action=ActionType.CLICK,
                 risk=RiskClass.RISKY, requires_confirmation=True,
                 risk_reviewed=risky_reviewed),
        ])


def test_approval_refused_while_a_risky_step_is_unreviewed(tmp_path):
    cat = Catalog(str(tmp_path))
    cat.save(_artifact(risky_reviewed=False))
    assert cat.unreviewed_risky_steps(cat.get("t.cap")) == [1]
    with pytest.raises(ValueError) as ex:
        cat.approve("t.cap")
    assert "[1]" in str(ex.value)
    assert cat.get("t.cap").approval_state.value == "draft"


def test_review_then_approve(tmp_path):
    cat = Catalog(str(tmp_path))
    cat.save(_artifact(risky_reviewed=False))
    cat.review_step("t.cap", 1, risk="risky", note="creates a real account")
    art = cat.get("t.cap")
    assert art.steps[1].risk_reviewed and "reviewed:" in art.steps[1].risk_reason
    assert cat.approve("t.cap").approval_state.value == "approved"


def test_reviewer_may_downgrade_a_false_positive(tmp_path):
    """A review gate that can only rubber-stamp is a checkbox. Downgrading an
    over-eager classification, with a recorded reason, is the gate working."""
    cat = Catalog(str(tmp_path))
    cat.save(_artifact(risky_reviewed=False))
    cat.review_step("t.cap", 1, risk="safe", note="renders a preview; no state change")
    art = cat.get("t.cap")
    assert art.steps[1].risk == RiskClass.SAFE
    assert art.steps[1].requires_confirmation is False
    assert cat.unreviewed_risky_steps(art) == []


def test_safe_steps_never_block_approval(tmp_path):
    cat = Catalog(str(tmp_path))
    art = _artifact(risky_reviewed=False)
    art.steps[1].risk = RiskClass.SAFE
    cat.save(art)
    assert cat.approve("t.cap").approval_state.value == "approved"
