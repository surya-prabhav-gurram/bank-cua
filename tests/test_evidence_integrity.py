"""
Evidence must agree with itself.

A run directory is the only account of what happened, and it is read by people
who were not there. Evidence that contradicts its own record is worse than no
evidence: it is unfalsifiable from the outside, and someone auditing it has no
way to tell which frames belong to the run they are reading about.

This was not hypothetical. Re-recording a capability wrote into the same
directory without clearing it, so a failed attempt's screenshots survived
alongside the successful run's summary -- leaving `step12..14` in a run whose
summary said 11 steps, and `escalation_step07` next to a summary saying
`success`. Both look entirely normal until someone cross-checks them.

The check is a cross-reference, not a picture: every screenshot's NAME is a claim
about an event, and the run's own summary and log either corroborate it or they
do not.
"""
import glob
import json
import os
import re

import pytest

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
EVIDENCE = os.path.join(ROOT, "evidence")


def _runs_with_screenshots():
    pattern = os.path.join(EVIDENCE, "**", "*.png")
    return sorted({os.path.dirname(p) for p in glob.glob(pattern, recursive=True)})


def _record(run_dir):
    summary_path = os.path.join(run_dir, "summary.json")
    if not os.path.exists(summary_path):
        return None, ""
    with open(summary_path) as fh:
        summary = json.load(fh)
    log_path = os.path.join(run_dir, "run.jsonl")
    log = open(log_path).read() if os.path.exists(log_path) else ""
    return summary, log


@pytest.fixture(scope="module")
def runs():
    found = _runs_with_screenshots()
    if not found:
        pytest.skip("no evidence captured; run scripts/gen_meridian_evidence.py")
    return found


def test_every_run_with_screenshots_has_a_summary_to_corroborate_them(runs):
    """Frames with no record are orphans -- usually a process killed mid-run.
    They are indistinguishable from evidence of something that happened."""
    orphans = [os.path.basename(d) for d in runs
               if not os.path.exists(os.path.join(d, "summary.json"))]
    assert not orphans, f"screenshots with no run record: {orphans}"


def test_a_business_outcome_screenshot_matches_the_outcome_that_was_reported(runs):
    """`business_MEMBER_NOT_ON_FILE.png` in a run that returned something else is
    a picture of one event filed under the name of another."""
    bad = []
    for run_dir in runs:
        summary, _log = _record(run_dir)
        if summary is None:
            continue
        code = (summary.get("business_outcome") or {}).get("code")
        for png in glob.glob(os.path.join(run_dir, "business_*.png")):
            claimed = os.path.basename(png)[len("business_"):-4]
            if claimed != code:
                bad.append(f"{os.path.basename(run_dir)}: {claimed} but summary "
                           f"says {code!r}")
    assert not bad, bad


def test_a_failure_screenshot_matches_the_failure_that_was_reported(runs):
    bad = []
    for run_dir in runs:
        summary, log = _record(run_dir)
        if summary is None:
            continue
        code = (summary.get("failure") or {}).get("code")
        for png in glob.glob(os.path.join(run_dir, "hard_*.png")):
            claimed = os.path.basename(png)[len("hard_"):-4]
            if claimed != code and f'"{claimed}"' not in log:
                bad.append(f"{os.path.basename(run_dir)}: {claimed} but summary "
                           f"says {code!r}")
    assert not bad, bad


def test_no_run_holds_a_screenshot_from_a_step_it_never_reached(runs):
    """The signature of a re-recording that inherited a previous attempt's
    frames: step12 in a run that took 11 steps."""
    bad = []
    for run_dir in runs:
        summary, _log = _record(run_dir)
        if summary is None:
            continue
        steps = summary.get("steps_executed", summary.get("num_steps", 0))
        if not steps:
            continue
        for png in glob.glob(os.path.join(run_dir, "step*.png")):
            match = re.fullmatch(r"step(\d+)", os.path.basename(png)[:-4])
            if match and int(match.group(1)) > steps:
                bad.append(f"{os.path.basename(run_dir)}: "
                           f"{os.path.basename(png)} but the run took {steps} steps")
    assert not bad, bad


def test_an_escalation_screenshot_only_appears_in_a_run_that_escalated(runs):
    """The other half of the same contamination: a frame captured while a
    previous attempt was escalating, left in a directory whose summary says the
    run succeeded."""
    bad = []
    for run_dir in runs:
        summary, _log = _record(run_dir)
        if summary is None:
            continue
        if glob.glob(os.path.join(run_dir, "escalation_step*.png")) \
                and summary.get("status") != "escalated":
            bad.append(f"{os.path.basename(run_dir)}: escalation frame in a "
                       f"{summary.get('status')!r} run")
    assert not bad, bad


def test_a_confirmation_screenshot_is_backed_by_an_escalation_in_the_same_log(runs):
    """`confirm_stepNN.png` says a human was asked to authorise something. The
    run's OWN log has to show that happening -- a coordinator wired to a
    different logger writes the escalation into somebody else's run."""
    bad = []
    for run_dir in runs:
        summary, log = _record(run_dir)
        if summary is None:
            continue
        if glob.glob(os.path.join(run_dir, "confirm_step*.png")) \
                and "escalation_raised" not in log:
            bad.append(os.path.basename(run_dir))
    assert not bad, f"confirmation frames with no escalation in their own log: {bad}"


def test_a_human_authorised_run_shows_the_outcome_and_not_only_the_question(runs):
    """A gated run that succeeded must carry evidence of what happened AFTER the
    approval, not just the prompt the operator was shown."""
    bad = []
    for run_dir in runs:
        summary, _log = _record(run_dir)
        if summary is None or summary.get("status") != "success":
            continue
        if not summary.get("intervention_id"):
            continue
        names = {os.path.basename(p) for p in glob.glob(os.path.join(run_dir, "*.png"))}
        if "completed_after_intervention.png" not in names:
            bad.append(f"{os.path.basename(run_dir)}: only {sorted(names)}")
    assert not bad, bad


def test_no_screenshot_is_empty(runs):
    """A zero-length capture reads as evidence and shows nothing."""
    tiny = [p for d in runs for p in glob.glob(os.path.join(d, "*.png"))
            if os.path.getsize(p) < 500]
    assert not tiny, [os.path.relpath(p, ROOT) for p in tiny]
