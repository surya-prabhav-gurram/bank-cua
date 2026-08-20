#!/usr/bin/env python3
"""
Record the seven Meridian capabilities by driving the live console.

What is real here and what is mocked
------------------------------------
Real: a live browser against https://web-sample.interface-hiring.com, the actual
discovery loop (observe -> decide -> act), locator synthesis from the live
elements, the safety pre-flight on every action, and the compiler that turns the
resulting transcript into a typed artifact.

Mocked: who chooses the action. Round 1's committed evidence is a genuine live
Anthropic run; these were recorded without an API key, so decisions come from the
traces below via `ScriptedProvider`. Swap `--provider anthropic` on
`bankcua.cli discover` and the same tasks record from a live model, because the
provider seam is what varies here and nothing else is.

Why the traces address controls by name
---------------------------------------
A trace of integer refs is unreadable and retargets silently when a page gains a
control. Naming the target keeps the recording reviewable and makes a mis-record
fail loudly rather than click the wrong button on a banking screen.

The target is shared and its global fault injection is set by whoever used it
last, so each recording resets it first -- see scripts/meridian_control.py for
why that belongs to the harness and not to the automation.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))
os.chdir(os.path.abspath(os.path.dirname(__file__) + "/.."))

from bankcua.agent.compiler import compile_artifact           # noqa: E402
from bankcua.agent.loop import DiscoveryLoop                   # noqa: E402
from bankcua.agent.providers import (ScriptedProvider,          # noqa: E402
                                     make_provider)
from bankcua.agent.task import DiscoveryTask                   # noqa: E402
from bankcua.catalog import Catalog                            # noqa: E402
from bankcua.escalation.handoff import (HandoffCoordinator,    # noqa: E402
                                        HandoffStore)
from bankcua.observability.logging import RunLogger            # noqa: E402
from bankcua.safety.policy import Policy, PolicyEngine         # noqa: E402
from bankcua.surface.web_playwright import WebSurface          # noqa: E402

POLICY = "config/policy.meridian.yaml"
TASKS = "config/tasks/meridian"

# Sign-on is a step inside every capability, not a capability others can reuse:
# each replay is a complete, independently auditable transaction, so there is no
# session to hand between invocations. The cost is one extra sign-on per call and
# it is stated in the write-up rather than hidden.
SIGNON = [
    {"action": "fill", "intent": "Enter the operator id",
     "target": {"near_label": "Operator ID:"}, "value": "{operator}"},
    {"action": "fill", "intent": "Enter the operator password",
     "target": {"near_label": "Password:"}, "value": "{password}"},
    {"action": "select", "intent": "Choose the branch",
     "target": {"near_label": "Branch:"}, "value": "{branch}", "select_by": "value"},
    {"action": "click", "intent": "Sign on to the console",
     "target": {"name": "Sign On"}},
]

# Both search modes land on the same results list, so member lookup selects from
# a one-row list rather than jumping straight to the record.
def _search(mode: str, value: str) -> list[dict]:
    return [
        {"action": "navigate", "intent": "Open Member Inquiry", "url": "/members"},
        {"action": "select", "intent": f"Search by {mode}",
         "target": {"near_label": "Search by:"}, "value": mode, "select_by": "value"},
        {"action": "fill", "intent": "Enter the search value",
         "target": {"near_label": "Value:"}, "value": value},
        {"action": "click", "intent": "Run the inquiry", "target": {"name": "Search"}},
    ]


TRACES: dict[str, list[dict]] = {
    "signon": [*SIGNON,
        {"action": "finish", "intent": "Reached the main menu", "success": True,
         "reason": "Operator session established"},
    ],

    "member_search": [*SIGNON, *_search("name", "{last_name}"),
        {"action": "extract", "intent": "Read the candidate member list",
         "target": {"readout": "Member No."}, "output_name": "matches",
         "attribute": "table"},
        {"action": "finish", "intent": "Candidates returned", "success": True,
         "reason": "Search results read; selection is the caller's decision"},
    ],

    "member_lookup": [*SIGNON, *_search("number", "{member_id}"),
        {"action": "click", "intent": "Select the matched member",
         "target": {"name": "Select"}},
        {"action": "extract", "intent": "Read the member name",
         "target": {"readout": "Name"}, "output_name": "member_name"},
        {"action": "extract", "intent": "Read the member e-mail",
         "target": {"readout": "E-mail"}, "output_name": "email"},
        {"action": "extract", "intent": "Read the member phone",
         "target": {"readout": "Phone"}, "output_name": "phone"},
        {"action": "extract", "intent": "Read every share with balance and status",
         "target": {"readout": "Share ID"}, "output_name": "shares",
         "attribute": "table"},
        {"action": "finish", "intent": "Record and balances read", "success": True,
         "reason": "Member record retrieved"},
    ],

    "transfer_funds": [*SIGNON,
        {"action": "navigate", "intent": "Open the funds transfer screen",
         "url": "/members/{member_id}/transfer"},
        {"action": "select", "intent": "Choose the source share",
         "target": {"near_label": "From Share:"}, "value": "{from_share}",
         "select_by": "value"},
        {"action": "select", "intent": "Choose the destination share",
         "target": {"near_label": "To Share:"}, "value": "{to_share}",
         "select_by": "value"},
        {"action": "fill", "intent": "Enter the amount",
         "target": {"near_label": "Amount:"}, "value": "{amount}"},
        {"action": "fill", "intent": "Enter the memo",
         "target": {"near_label": "Memo:"}, "value": "{memo}"},
        {"action": "click", "intent": "Continue to the review screen",
         "target": {"name": "Continue"}},
        # Irreversible. The caption carries no risky keyword; it is the POST-form
        # structural signal that classifies this, and a human ratifies it before
        # the capability can be approved.
        {"action": "click", "intent": "Post the transfer",
         "target": {"name": "Post Transfer"}},
        {"action": "extract", "intent": "Read the host confirmation number",
         "target": {"readout": "Confirmation"}, "output_name": "confirmation"},
        {"action": "finish", "intent": "Transfer posted", "success": True,
         "reason": "Host returned a confirmation number"},
    ],

    "open_share": [*SIGNON,
        {"action": "navigate", "intent": "Open the new share screen",
         "url": "/members/{member_id}/open-share"},
        # by VALUE, not label: Meridian's option labels are "MMKT - Money
        # Market" while the value is the bare code, and a recording that names
        # the wrong one fails at replay rather than at record time.
        {"action": "select", "intent": "Choose the share product",
         "target": {"near_label": "Share Type:"}, "value": "{share_type}",
         "select_by": "value"},
        {"action": "fill", "intent": "Enter the initial deposit",
         "target": {"near_label": "Initial Deposit:"}, "value": "{initial_deposit}"},
        {"action": "click", "intent": "Continue to the review screen",
         "target": {"name": "Continue"}},
        {"action": "click", "intent": "Establish the new share",
         "target": {"name": "Open Share"}},
        {"action": "extract", "intent": "Read the host confirmation number",
         "target": {"readout": "Confirmation"}, "output_name": "confirmation"},
        {"action": "finish", "intent": "Share established", "success": True,
         "reason": "Host returned a confirmation number"},
    ],

    "update_member_info": [*SIGNON,
        {"action": "navigate", "intent": "Open the member update screen",
         "url": "/members/{member_id}/update"},
        {"action": "fill", "intent": "Set the e-mail",
         "target": {"near_label": "E-mail:"}, "value": "{email}"},
        {"action": "fill", "intent": "Set the phone",
         "target": {"near_label": "Phone:"}, "value": "{phone}"},
        {"action": "fill", "intent": "Set the mailing address",
         "target": {"near_label": "Mailing Address:"}, "value": "{address}"},
        {"action": "click", "intent": "Save the contact changes",
         "target": {"name": "Save Changes"}},
        # The acknowledgement banner says a write was accepted. Reading the
        # record back says it landed -- and the record is what the next caller
        # will see.
        {"action": "navigate", "intent": "Reopen the member record to verify",
         "url": "/members/{member_id}"},
        {"action": "extract", "intent": "Read back the stored e-mail",
         "target": {"readout": "E-mail"}, "output_name": "email"},
        {"action": "extract", "intent": "Read back the stored phone",
         "target": {"readout": "Phone"}, "output_name": "phone"},
        {"action": "finish", "intent": "Contact details updated and verified",
         "success": True, "reason": "Stored record shows the new values"},
    ],

    "place_hold": [*SIGNON,
        {"action": "navigate", "intent": "Open the account hold screen",
         "url": "/members/{member_id}/hold"},
        {"action": "select", "intent": "Choose the share to restrict",
         "target": {"near_label": "Share:"}, "value": "{share_id}",
         "select_by": "value"},
        {"action": "select", "intent": "Choose the hold reason code",
         "target": {"near_label": "Reason Code:"}, "value": "{reason_code}",
         "select_by": "value"},
        {"action": "fill", "intent": "Enter the hold notes",
         "target": {"near_label": "Notes:"}, "value": "{notes}"},
        {"action": "click", "intent": "Continue to the review screen",
         "target": {"name": "Continue"}},
        {"action": "click", "intent": "Apply the hold to the share",
         "target": {"name": "Apply Hold"}},
        {"action": "extract", "intent": "Read the host confirmation number",
         "target": {"readout": "Confirmation"}, "output_name": "confirmation"},
        {"action": "finish", "intent": "Hold applied", "success": True,
         "reason": "Host returned a confirmation number"},
    ],
}


def record(key: str, evidence_root: str, out_dir: str, headed: bool = False,
           provider_kind: str = "scripted", model: str | None = None) -> str | None:
    task = DiscoveryTask.load(os.path.join(TASKS, f"{key}.json"))
    secret_names = {p.name for p in task.inputs if p.sensitive}
    secrets = {n: str(task.param_values[n]) for n in secret_names
               if task.param_values.get(n)}
    suffix = "" if provider_kind == "scripted" else f"-{provider_kind}"
    run_id = f"discovery-{task.capability_id}{suffix}"
    run_dir = os.path.join(evidence_root, run_id)
    # Clear it first. A re-recording writes into the same directory, and a
    # failed attempt leaves its screenshots behind -- so the directory ends up
    # holding step12..14 from a run that took 11 steps, and an
    # escalation_step07 next to a summary saying `success`. Evidence that
    # contradicts its own record is worse than no evidence: it is unfalsifiable
    # from the outside, and someone auditing it has no way to tell which frames
    # belong to the run they are reading about.
    if os.path.isdir(run_dir):
        shutil.rmtree(run_dir)
    logger = RunLogger(run_dir, "discovery", secret_names, secrets)

    policy = PolicyEngine(Policy.from_yaml(POLICY),
                          # Discovery must be allowed to walk THROUGH the
                          # irreversible step, or the flow can never be recorded
                          # past the review screen. Replay of the resulting
                          # artifact is gated separately and stays fail-closed.
                          allow_risky_override=True)
    surface = WebSurface(task.base_url, headless=not headed)
    surface.start()
    try:
        provider = (ScriptedProvider(TRACES[key]) if provider_kind == "scripted"
                    else make_provider(provider_kind, model=model))
        loop = DiscoveryLoop(surface, provider, policy, logger,
                             HandoffCoordinator(HandoffStore("evidence/handoffs"),
                                                logger))
        result = loop.run(task)
        summary = {"status": result.status, "reason": result.reason,
                   "outputs": result.outputs, "num_steps": len(result.transcript)}
        path = None
        if result.status == "success":
            art = compile_artifact(task, result, evidence_dir=run_dir,
                                   recorded_by=provider.name if provider_kind
                                   != "scripted" else "scripted-trace",
                                   discovery_run_id=run_id)
            path = Catalog(out_dir).save(art)
            summary["artifact_path"] = path
        logger.finish(summary)
        status = result.status.upper()
        print(f"  [{key:20}] {status:9} steps={len(result.transcript)} "
              f"outputs={sorted(result.outputs)} {result.reason[:60]}")
        return path
    finally:
        surface.stop()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="record_meridian")
    ap.add_argument("--only", action="append", choices=sorted(TRACES),
                    help="record just these capabilities (repeatable)")
    ap.add_argument("--out", default="capabilities/meridian")
    ap.add_argument("--evidence", default="evidence/meridian")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--provider", default="scripted",
                    choices=["scripted", "anthropic"],
                    help="'anthropic' drives discovery with a live model and "
                         "needs ANTHROPIC_API_KEY; 'scripted' replays the "
                         "recorded decision traces in this file")
    ap.add_argument("--model", default="claude-sonnet-5")
    ap.add_argument("--no-reset", action="store_true",
                    help="skip clearing the target's global fault injection")
    args = ap.parse_args(argv)

    if not args.no_reset:
        from meridian_control import TargetControl
        state = TargetControl().signon().apply("0.0", "")
        print(f"[target] fault injection cleared -> {state['error_rate']!r} "
              f"{state['forced_inject']!r}")

    keys = args.only or list(TRACES)
    made = []
    for k in keys:
        # A mis-recorded trace is a bug in the trace, not a reason to abandon the
        # other six: report it and carry on, so one run shows every problem
        # rather than only the first.
        try:
            made.append(record(k, args.evidence, args.out, args.headed,
                               args.provider, args.model))
        except Exception as ex:
            print(f"  [{k:20}] ERROR     {type(ex).__name__}: {str(ex)[:110]}")
            made.append(None)
    ok = [m for m in made if m]
    print(f"[record] {len(ok)}/{len(keys)} capabilities recorded into {args.out}")
    return 0 if len(ok) == len(keys) else 1


if __name__ == "__main__":
    raise SystemExit(main())
