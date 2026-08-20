"""
Drift-driven repair: detect → propose → a human approves → apply.

The behaviour worth pinning is not that it can reorder a list. It is that it
knows which repairs make the artifact BETTER and refuses the ones that merely
make the symptom go away.
"""

from bankcua.repair import (DriftLedger, ProposalStore, analyse,
                            apply as apply_repair)
from bankcua.replay.result import DriftSignal, ReplayResult, ReplayStatus
from bankcua.schema import (ActionType, CapabilityArtifact, Checkpoint,
                            Locator, LocatorCandidate, LocatorKind, Step, Target)


def _artifact(primary_kind=LocatorKind.ROLE):
    loc = Locator(description="the Search button", candidates=[
        LocatorCandidate(kind=primary_kind, role="button", value="Search"),
        LocatorCandidate(kind=LocatorKind.TEXT, value="Find"),
        LocatorCandidate(kind=LocatorKind.CSS, value="td > input"),
    ])
    return CapabilityArtifact(
        id="t.cap", name="t", description="t", version="1.4.2",
        target=Target(app_id="a", base_url="http://127.0.0.1:1"),
        success=Checkpoint(kind="url_matches", value="/x"),
        steps=[Step(index=0, intent="click search", action=ActionType.CLICK,
                    target=loc)])


def _result(candidate_index, kind):
    return ReplayResult(status=ReplayStatus.SUCCESS, capability_id="t.cap",
                        version="1.4.2",
                        drifts=[DriftSignal(step_index=0, description="the Search button",
                                            candidate_index=candidate_index, kind=kind)])


def _ledger(tmp_path, n, candidate_index, kind):
    led = DriftLedger(str(tmp_path / "drift.jsonl"))
    for _ in range(n):
        led.record_result(_result(candidate_index, kind))
    return led


# ---- one drift is noise, a trend is a signal -----------------------------
def test_below_threshold_proposes_nothing(tmp_path):
    art = _artifact()
    p = analyse(art, _ledger(tmp_path, 2, 1, "text"), min_occurrences=3)
    assert p.repairs == [] and p.unrepairable == []
    assert "nothing to propose" in p.summary()


def test_repeated_drift_to_another_semantic_strategy_is_proposed(tmp_path):
    art = _artifact()
    p = analyse(art, _ledger(tmp_path, 4, 1, "text"), min_occurrences=3)
    assert len(p.repairs) == 1
    r = p.repairs[0]
    assert r.step_index == 0 and r.to_candidate == 1 and r.occurrences == 4


# ---- the judgement that stops it making things worse ---------------------
def test_semantic_to_structural_drift_is_refused_and_explained(tmp_path):
    """Falling back from role/near_label to CSS means the control was RENAMED.
    Promoting the CSS path would trade a strategy that survives rebranding for one
    that breaks on the next layout change -- a fix that makes the artifact worse."""
    art = _artifact()
    p = analyse(art, _ledger(tmp_path, 5, 2, "css"), min_occurrences=3)
    assert p.repairs == []
    assert len(p.unrepairable) == 1
    msg = p.unrepairable[0]
    assert "RENAMED" in msg and "label_map" in msg


def test_missing_candidate_is_reported_not_guessed(tmp_path):
    art = _artifact()
    art.steps[0].target.candidates = art.steps[0].target.candidates[:1]
    p = analyse(art, _ledger(tmp_path, 4, 2, "css"), min_occurrences=3)
    assert p.repairs == []
    assert "re-record" in p.unrepairable[0]


# ---- applying borrows the existing governance ----------------------------
def test_apply_promotes_bumps_version_and_returns_to_draft(tmp_path):
    art = _artifact()
    art.approval_state = art.approval_state.__class__("approved")
    p = analyse(art, _ledger(tmp_path, 4, 1, "text"), min_occurrences=3)
    out = apply_repair(art, p)

    assert out.steps[0].target.candidates[0].kind == LocatorKind.TEXT
    assert "promoted by drift repair" in out.steps[0].target.candidates[0].reasoning
    assert out.version == "1.4.3" and p.to_version == "1.4.3"
    # an edited flow must re-earn approval and its stability score
    assert out.approval_state.value == "draft"
    assert out.stability is None


def test_apply_does_not_mutate_the_original(tmp_path):
    art = _artifact()
    p = analyse(art, _ledger(tmp_path, 4, 1, "text"), min_occurrences=3)
    apply_repair(art, p)
    assert art.steps[0].target.candidates[0].kind == LocatorKind.ROLE
    assert art.version == "1.4.2"


# ---- persistence ---------------------------------------------------------
def test_proposals_round_trip(tmp_path):
    art = _artifact()
    p = analyse(art, _ledger(tmp_path, 4, 1, "text"), min_occurrences=3)
    store = ProposalStore(str(tmp_path / "repairs"))
    store.save(p)
    assert [x.id for x in store.list()] == [p.id]
    assert store.load(p.id).repairs[0].to_candidate == 1


def test_ledger_ignores_corrupt_lines(tmp_path):
    """A truncated write must not silently disable drift detection."""
    path = tmp_path / "drift.jsonl"
    led = DriftLedger(str(path))
    led.record_result(_result(1, "text"))
    with open(path, "a") as f:
        f.write("{not json\n")
    led.record_result(_result(1, "text"))
    assert len(led.observations("t.cap")) == 2
