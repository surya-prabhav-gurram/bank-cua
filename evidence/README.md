# Evidence

Structured logs and richer signals from real discovery and replay runs. All text
is redacted (no secrets/PII); screenshots are treated as sensitive evidence.

## Discovery (GENUINE live LLM run via the Anthropic API)
- `discovery-member_savings_lookup-anthropic-live/` — member + balance lookup
  (recorded_by: anthropic):
  `run.jsonl`, `transcript.json` (redacted), `step00..07.png`, `summary.json`,
  and `bridge_trace/` (the exact per-step model decision requests/responses).
- `discovery-open_subaccount-anthropic-live/` — sub-account creation via the live
  API (incl. the irreversible confirm step).

## Replay (deterministic, no LLM) — the result contract
- `replay-01-success/` — SUCCESS with typed outputs (`savings_balance` in cents).
- `replay-02-not-found/` — BUSINESS_OUTCOME `MEMBER_NOT_FOUND`.
- `replay-03-permission-denied/` — BUSINESS_OUTCOME `PERMISSION_DENIED`, and the
  outcome still carries data: Corebank's denial screen withholds the balance but
  names the member, so `member_name` is returned with
  `outputs_surfaced: ["member_name"]` (see `KnownCondition.surfaces_outputs`).
- `replay-04-interstitial-recovered/` — RECOVERABLE: interstitial auto-dismissed;
  run still SUCCEEDS (see `summary.json.recoveries`).
- `replay-05-session-timeout/` — HARD_FAILURE `SESSION_TIMEOUT` w/ screenshot + DOM.
- `replay-10-fill-not-applied/` — HARD_FAILURE `FILL_NOT_APPLIED`. The injected
  input accepts the keystrokes and discards them, so the action reports success
  and the page looks entirely normal: no page-state checkpoint can see this. Only
  reading the control back does (`Step.verify_value`).

## Value-level (semantic) policy — amounts and dual control
These are `status: refused`, not `failure`. Nothing broke: a guardrail declined,
and the caller's move is to change the request (a smaller amount, a second
approver), not to investigate the system. Compare `replay-10`, which is a genuine
`failure` because the automation could not proceed.
The URL/action allowlist cannot tell $1 from $1M. These rules read the
invocation's *inputs* before the browser opens.
- `replay-11-value-limit-exceeded/` — `VALUE_LIMIT_EXCEEDED`: a $25,000 deposit
  against a $10,000 ceiling. Refused outright, never escalated, `steps_executed: 0`
  — nothing was opened and nothing was typed.
- `replay-12-dual-control-unmet/` — `DUAL_CONTROL_REQUIRED`: $1,500 is over the
  dual-control threshold and nobody counter-signed. Unattended is precisely when a
  second pair of eyes cannot be assumed, so it fails closed.
- `replay-13-dual-control-countersigned/` — the same $1,500 with an independent
  approver (`--initiator alice --approver bruce`) clears the *value* gate
  (`dual_control_satisfied` in `run.jsonl`) and is then stopped by the *step* gate
  on the irreversible click (`CONFIRMATION_REQUIRED`). Two independent guardrails,
  both doing their job. A run cannot approve itself.

## Escalation & handoff
- `escalation-06-handoff/` — replay of the irreversible sub-account flow pauses,
  a human operator takes control of the SAME live session over CDP, performs the
  confirm, hands back; replay resumes to SUCCESS.
- `handoffs/…step9.json` — the resolved intervention (human_actions + control token).

## Cross-tenant reuse (same artifact, second tenant)
- `replay-07-crosstenant-summit-override/` — the member-lookup artifact recorded on
  tenant `demo-cu` replayed against tenant `summit-cu` (rebranded labels) using a
  ~4-line string override: clean SUCCESS, zero drift.
- `replay-08-crosstenant-summit-nomap/` — the SAME artifact against Summit with NO
  override: still SUCCEEDS via structural locator fallbacks, emitting drift signals
  at the label/role-based steps (graceful degradation, see `summary.json.drifts`).

## A second surface (the `Surface` seam, demonstrated)
- `replay-14-second-surface-a11y/` — the SAME artifact recorded on the Playwright
  surface, replayed through one that has no DOM: it perceives an accessibility
  tree and acts with a mouse and keyboard, the way a desktop UIA/AX driver does.
  Identical outputs, zero drift. `portability.json` alongside is the static
  answer to "can this artifact run here?", computed before anything launched.

## Confidence
- `replay-09-stability/` — the capability replayed N times; pass rate reported
  (flakiness signal; feeds the draft→approved gate).

Regenerate everything: `python scripts/gen_evidence.py` (starts its own tenants).
Regenerate discovery: `bash scripts/run_discovery.sh`.
