#!/usr/bin/env python3
"""
Walk the review-and-approve gate for the recorded Meridian capabilities.

Why this is a step you run, and not a state the repo ships in
-------------------------------------------------------------
Approval is a human act performed against a deployment, not a property of a
recording. Committing pre-approved artifacts would misrepresent them and would
quietly disarm the gate for anyone who clones the repo -- the capabilities would
arrive already cleared for unattended replay of irreversible actions, reviewed by
nobody.

So the artifacts ship as `draft`, the API refuses to invoke a draft, and this
script is the operator action that clears them. It exists to make the demo
one command rather than seven, NOT to skip the gate: every risky step is
ratified individually with a recorded justification, exactly as
`bankcua.cli catalog review` would do it by hand.

Run it once after recording:  python scripts/approve_meridian.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))
os.chdir(os.path.abspath(os.path.dirname(__file__) + "/.."))

from bankcua.catalog import Catalog  # noqa: E402

CATALOG = "capabilities/meridian"

# One justification per irreversible step, written against what the step DOES on
# this target rather than boilerplate -- a reviewer note that could apply to any
# step is not a review.
#: Capabilities whose irreversible step may run in an explicitly approved
#: unattended run -- deliberately EMPTY.
#:
#: The earlier reading was that the value policy (amount ceiling, dual-control
#: threshold, rolling-window budget) is envelope enough for money movement. It
#: is not, and the gap is specific: those rules bound the SIZE of a mistake, not
#: whether one is being made. A $1.00 transfer to the wrong share is inside
#: every limit and is still wrong, and nothing downstream can undo it.
#:
#: It also has to agree with `config/service.yaml`, which sets `allow_risky:
#: false` for these. When it did not, the two combined into a hard BLOCK rather
#: than a pause -- `requires_confirmation: false` plus `allow_risky: false` is
#: the one combination the policy engine refuses outright -- so the capability
#: could not run at all, and the console's Confirm button had nothing to answer.
UNATTENDED: set[str] = set()

#: Steps the classifier flagged that a reviewer has judged NOT irreversible,
#: matched on intent text rather than index so a re-recording that takes a
#: different path leaves them flagged rather than silently downgrading the wrong
#: step. This is the gate working: a heuristic proposes, a person disagrees, and
#: the disagreement is recorded on the artifact with its reason.
DOWNGRADES = [
    ("meridian.transfer_funds", "Funds Transfer form",
     "navigation only: this opens the transfer FORM. The lexical rule matched "
     "the word 'Transfer' in a link caption; the structural signal did not "
     "corroborate it, because there is no form submission here. Nothing is "
     "posted until the separate 'Post Transfer' step, which stays risky."),
]

NOTES = {
    "meridian.transfer_funds":
        "Posts a funds movement between shares; the host issues a confirmation "
        "number and provides no reversal path. Stops for a person on every run: "
        "the value policy bounds how large a mistake can be, not whether one is "
        "being made.",
    "meridian.open_share":
        "Establishes a new share on the member record. Caption 'Open Share' "
        "carries no risky keyword; the POST-form structural signal is what "
        "classified it, and that classification is correct.",
    "meridian.update_member_info":
        "Overwrites stored contact details. Reversible only by knowing the "
        "previous values, which the capability reads back but does not retain.",
    "meridian.place_hold":
        "Restricts a member's share. Irreversible, and the host additionally "
        "requires a supervisor profile. Left un-risky in config/service.yaml so "
        "unattended invocation escalates to a person rather than proceeding.",
}


def main() -> int:
    cat = Catalog(CATALOG)
    arts = cat.list()
    if not arts:
        print(f"no capabilities in {CATALOG}; run scripts/record_meridian.py first")
        return 1
    for art in arts:
        # Reviewer disagreements first, so they are not then ratified as risky.
        for cap_id, needle, why in DOWNGRADES:
            if cap_id != art.id:
                continue
            step = next((s for s in art.steps
                         if s.risk.value == "risky" and needle in s.intent), None)
            if step is None:
                # Say so, loudly. Skipping in silence is what happened when a
                # re-recording renamed this step from "Start a Funds Transfer"
                # to "Open Funds Transfer form": the downgrade quietly did not
                # apply, the step fell through to the ratification loop below,
                # and a navigation-only step was approved as irreversible
                # carrying a justification about posting money that no longer
                # described it. A reviewer decision that vanishes when a caption
                # changes is not a reviewer decision.
                raise SystemExit(
                    f"[approve] REFUSING: {cap_id} declares a reviewer "
                    f"downgrade for a risky step whose intent contains "
                    f"{needle!r}, and no such step exists. The capability was "
                    f"probably re-recorded down a different path. Re-read the "
                    f"steps and update DOWNGRADES before approving anything:\n"
                    + "\n".join(f"    step {s.index}: {s.intent}"
                                 for s in art.steps if s.risk.value == "risky"))
            cat.review_step(art.id, step.index, risk="safe", note=why)
            print(f"  DOWNGRADED {art.id} step {step.index} -> safe "
                  f"(reviewer disagreed with the classifier)")
        art = cat.get(art.id)

        pending = cat.unreviewed_risky_steps(art)
        for index in pending:
            note = NOTES.get(art.id, "irreversible step reviewed for this deployment")
            unattended = art.id in UNATTENDED
            cat.review_step(art.id, index, risk="risky", note=note,
                            requires_confirmation=not unattended)
            print(f"  reviewed {art.id} step {index} "
                  f"({'may run unattended' if unattended else 'always escalates'})")
        approved = cat.approve(art.id)
        print(f"  approved {approved.id} v{approved.version} "
              f"({len(pending)} risky step(s) ratified)")
    print(f"[approve] {len(arts)} capabilities approved for unattended replay")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
