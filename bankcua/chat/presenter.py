"""
Turn a ReplayResult into a sentence a bank operator can act on.

Why this is its own module
--------------------------
The result contract spends real effort separating four things a caller must not
confuse: the goal was reached, the application gave a legitimate non-happy answer,
a guardrail declined, or something actually broke. A presenter that renders all
four as "Sorry, something went wrong" throws that distinction away at the last
possible moment -- and the chatbot is the only part of the system most people
will ever see, so the distinction survives here or it does not survive at all.

Each status therefore gets not just different words but a different NEXT STEP,
because that is what the distinction is for:

  success           -> here is the data
  business_outcome  -> the bank answered; here is what it said and what it means
  refused           -> nothing was attempted; here is what would have to change
  escalated         -> it is paused on a live session waiting for a person
  failure           -> it broke; here is the step, and an engineer needs this

The one thing this module must never do is soften a refusal into an apology or
dress a failure as a result.
"""
from __future__ import annotations

from typing import Any

#: Plain-language readings of the codes the Meridian taxonomy can return. Held
#: as data next to the taxonomy it mirrors, so adding a condition is one edit in
#: config/knowledge/ and one here -- and a code with no entry still renders
#: correctly, just less warmly.
OUTCOME_TEXT: dict[str, str] = {
    "MEMBER_NOT_ON_FILE":
        "There is no member record for that number.",
    "AMBIGUOUS_MEMBER_MATCH":
        "Several member records matched that number, so I stopped rather than "
        "guess which one you meant — acting on the wrong member's account is the "
        "one mistake worth refusing to make. Give me the full member number, or "
        "ask me to search by last name and I will list the candidates.",
    "MEMBER_NUMBER_NOT_EXACT":
        "That is not a complete member number — the search matched it as a "
        "partial and landed on a different member, so I read nothing. Give me "
        "the full number, or ask me to search by last name.",
    "NO_SEARCH_MATCHES":
        "No member records matched that search.",
    "SUPERVISOR_OVERRIDE_REQUIRED":
        "The host will not let a teller complete this. A supervisor has to "
        "perform it.",
    "INSUFFICIENT_FUNDS":
        "The source share does not have enough available balance.",
    "SOURCE_SHARE_ON_HOLD":
        "The source share is under a hold and cannot be debited. Another share "
        "would have to be used.",
    "REQUEST_NOT_VALIDATED":
        "The host would not accept those details — the screen says which rule "
        "they broke, such as a minimum opening deposit. Nothing was posted, so "
        "correcting the value and asking again is all that is needed.",
    "TRANSACTION_REJECTED":
        "The host rejected the transaction as entered.",
    "SESSION_TIMED_OUT":
        "The operator session expired part-way through, so the request was "
        "stopped rather than resumed -- whether the last step took effect has "
        "to be confirmed before retrying.",
}

REFUSAL_TEXT: dict[str, str] = {
    "VALUE_LIMIT_EXCEEDED":
        "That amount is outside the limits configured for this system, so "
        "nothing was opened or submitted.",
    "DUAL_CONTROL_REQUIRED":
        "That amount needs a second, independent approver before it can go "
        "ahead.",
    "CONFIRMATION_REQUIRED":
        "That step is irreversible and needs a person to confirm it. Nothing "
        "was posted.",
    "ROLE_NOT_PERMITTED":
        "That function is restricted to supervisors.",
    "OPERATOR_NOT_PERMITTED":
        "That operator is not permitted to run this capability.",
    "UNKNOWN_OPERATOR":
        "I do not recognise that operator.",
    "CAPABILITY_NOT_APPROVED":
        "That capability has not been approved for use yet. A reviewer has to "
        "sign it off first.",
    "STEP_BLOCKED_BY_POLICY":
        "A guardrail blocked an irreversible step in that flow.",
    "OPERATOR_REQUIRED":
        "I need to know which operator to act as.",
    "MISSING_REQUIRED_INPUT":
        "That capability needs an argument the request did not supply, so "
        "nothing was opened.",
}


def _money_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "  (none)"
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    head = "  " + "  ".join(c.ljust(widths[c]) for c in cols)
    body = ["  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols)
            for r in rows]
    return "\n".join([head, "  " + "-" * (len(head) - 2), *body])


def render_outputs(outputs: dict[str, Any]) -> str:
    """Scalars as lines, grids as grids.

    A table flattened into `{'shares': [{...}, {...}]}` is technically the answer
    and practically unreadable, which defeats the point of having typed rows.
    """
    if not outputs:
        return ""
    lines = []
    for name, value in outputs.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{name}:")
            lines.append(_money_rows(value))
        else:
            lines.append(f"{name}: {value}")
    return "\n".join(lines)


def present(result: dict) -> str:
    """Render one invocation result. `result` is the API's JSON body."""
    status = result.get("status", "unknown")
    outputs = result.get("outputs") or {}

    if status == "success":
        rendered = render_outputs(outputs)
        return ("Done." if not rendered else f"Done.\n\n{rendered}")

    if status == "business_outcome":
        outcome = result.get("business_outcome") or {}
        code = outcome.get("code", "")
        # An outcome is an ANSWER, so it leads with what the bank said -- not
        # with an apology, which would read as a malfunction.
        text = OUTCOME_TEXT.get(code) or outcome.get("message") or code
        parts = [text]
        surfaced = outcome.get("outputs_surfaced") or []
        if surfaced and outputs:
            parts.append("The host did still return:\n"
                         + render_outputs({k: outputs[k] for k in surfaced
                                           if k in outputs}))
        parts.append(f"(outcome: {code})")
        return "\n\n".join(parts)

    if status == "refused":
        refusal = result.get("refusal") or {}
        code = refusal.get("code", "")
        text = REFUSAL_TEXT.get(code) or refusal.get("reason") or code
        parts = [f"I did not do that. {text}"]
        # The requirement is the actionable half: it says what would have to be
        # true, which is the difference between a refusal and a dead end.
        if refusal.get("requirement"):
            parts.append(f"To proceed it would need: {refusal['requirement']}.")
        parts.append(f"(refused: {code} — nothing was submitted)")
        return "\n\n".join(parts)

    if status == "escalated":
        iid = result.get("intervention_id") or "the open intervention"
        return ("I stopped and handed this to a person. The session is still "
                f"open and waiting for an operator to take control ({iid}). "
                "Nothing was completed automatically.")

    failure = result.get("failure") or {}
    code = failure.get("code", "unknown")
    step = failure.get("step_index")
    where = f" at step {step}" if step is not None else ""
    detail = ""
    if failure.get("expected"):
        detail = (f"\n\nExpected: {failure['expected']}"
                  f"\nObserved: {failure.get('observed', '')}")
    # A failure is the only status that should sound like something is wrong,
    # and it names the step because that is what an engineer needs first.
    return (f"That did not complete{where} — this looks like a fault rather than "
            f"an answer from the bank, so it needs investigating. "
            f"(failure: {code}){detail}")
