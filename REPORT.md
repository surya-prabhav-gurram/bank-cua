# REPORT

A record-once / replay-many computer-use system for driving legacy bank
back-office UIs. An LLM discovers a flow once; the run is compiled into a typed,
versioned **capability artifact**; that artifact is replayed deterministically
with no model in the loop, with an explicit runtime-error contract, safety
guardrails, and a human-handoff seam. This document explains the design and the
trade-offs behind it.

## 1. Architecture

The system is one Python package with sharp internal seams and a single
dependency direction: everything speaks the **schema** and the **surface**
interface; the only places that speak Playwright are the web surface and the operator's CDP client (the handoff).

```
goal + target ─▶ Discovery loop ──▶ Transcript ──▶ Compiler ──▶ Capability artifact (JSON)
                 (LLM: observe/decide/act)                          │
                                                                    ▼
input params ─────────────────────────────────▶ Replay engine (NO LLM) ─▶ ReplayResult
                                                    │  ▲
                                    guardrails ◀────┘  └──▶ Escalation / live-session handoff
```

Key boundaries and why they are where they are:

- **`Surface` (perceive/act) vs. the recorded flow.** `surface/base.py` defines
  perception (`observe`, `index_elements`, `index_readouts`) and Locator-based
  actions (`click/fill/select/read/check/detect`). The artifact schema, the
  discovery loop, and the replay engine import *only* this interface. Swapping in
  a legacy-web frame driver or a desktop accessibility driver is a new `Surface`
  implementation and touches nothing else. This is the seam that keeps §4
  credible.
- **Discovery and replay share the same targeting primitives.** Discovery
  synthesises a Locator from the live element it chose and acts *through* that
  Locator — so the exact targeting that gets recorded is the exact targeting that
  gets exercised. What we record is not a guess; it already worked once.
- **The model never writes selectors.** It picks elements by an integer `ref`
  from the observation; the system computes the robust, multi-strategy Locator.
  This keeps the model's job small and makes locator quality a property of code,
  not of prompt luck.
- **The compiler is the record→capability boundary.** It converts a transcript
  into a typed artifact: parameterising values/URLs, synthesising per-step
  checkpoints, attaching the vendor's known-condition library, and referencing
  (not inlining) a redacted transcript.
- **Single process, synchronous, file-backed.** No queue, no services. The brief
  explicitly does not reward scaling plumbing; the abstractions (artifact as
  data, catalog, handoff inbox) are the parts that would scale, and they are
  already serialisable and stateless. Simplicity is the deliberate choice.

Stack: Python + Playwright (sync), Pydantic v2 for the schema, Flask for the
mock target. Playwright because it gives a role/accessibility locator model that
ports conceptually to a desktop accessibility tree, robust waiting, frame
handling, and a CDP endpoint we reuse for the handoff.

## 2. Artifact schema

`schema.py` is the focal point: a `CapabilityArtifact` is a **contract**, not a
step dump. A calling agent should understand what it needs and returns without
reading the steps; a human should be able to review it.

- **Contract-first.** Typed `inputs` (with `required`, `sensitive`, `example`),
  typed `outputs`, and a top-level `success` checkpoint state the interface. The
  `catalog` turns these into a function-calling manifest an agent invokes by
  name.
- **Robust targeting as data.** Every control is a frame-aware `Locator` with an
  **ordered list of candidate strategies**, most-stable-first, each carrying a
  human `reasoning` string. Preference order is semantic → structural: role+name,
  label, placeholder, link text, then CSS, then XPath. Replay uses the first
  candidate that resolves; one strategy going stale doesn't break the step.
  `frame_path` addresses controls inside iframes (the balance pane), and read-only
  values (member name, balance) get **label-relative XPath** locators
  (`//tr[td="Savings"]/td[last()]`) that survive table reflow.
- **The error taxonomy lives in the schema.** `KnownCondition` records, as data,
  a detector, a class (business_outcome / recoverable / hard_failure), and an
  optional recovery. This makes the most important behaviour reviewable and
  reusable rather than buried in code.
- **Safety in the schema.** Each `Step` carries a `RiskClass` and a
  `requires_confirmation` flag; the compiler marks create/confirm/submit steps
  risky so policy and replay can gate them.
- **Reuse-ready target.** `Target` separates the *shared flow* (steps, locators)
  from the *per-tenant binding* (`base_url`, `tenant_id`, `vendor_product`,
  `version`). Re-pointing an artifact at another tenant is a binding change, not a
  re-record (§4).
- **Provenance, decoupled from the transcript.** `version` (semver),
  `approval_state` (draft/approved), `recorded_by`, and a `transcript_ref` +
  `sha256` — the raw transcript is stored separately and redacted, never inlined.

Everything is Pydantic, so it validates on load and serialises to reviewable JSON
(`capabilities/*.json`).

## 3. Determinism & error handling

**Determinism.** Replay runs with no model. It (a) resolves each control via the
recorded ordered candidates, (b) waits for declared conditions rather than
sleeping, and (c) asserts a **per-step checkpoint** after acting — the "did the
click actually work" guard — before proceeding. Parameters are substituted into
URLs, values, and checkpoints at run time, so `/member?mid={member_id}` and its
checkpoint track the supplied input.

**The runtime-error contract is the core.** Because these UIs are stable, the
interesting failures are runtime conditions, and conflating a legitimate outcome
with a crash is the classic mistake. After every step (and on any action
failure) replay runs the artifact's `KnownCondition` detectors and acts on the
first match:

- **refused** → a guardrail declined before or during the run; `{status: refused,
  refusal: {code, requirement, reason}}`, where `requirement` states what would
  have to be true for it to proceed. Over HTTP this is a 403, not the 422 used
  for a run that broke.
- **business_outcome** → stop and return `{status: business_outcome, code}`.
  "No such member" and "permission denied" are *answers the caller wants*, not
  errors. (`ReplayStatus.BUSINESS_OUTCOME`, never `FAILURE`.) A condition may
  also declare `surfaces_outputs`: some non-successes still carry data. Corebank's
  denial screen withholds the balance but still identifies the member, which is
  what the caller needs to route the request; "not found" has nothing to give.
  Which of the two a condition is, is a property of the vendor's UI, so it is
  declared per condition in the shared library rather than guessed at runtime, and
  the surfaced names are reported separately (`outputs_surfaced`) so partial data
  never reads as a success.
- **recoverable** → run the declared recovery (dismiss an interstitial, reload a
  transient 500) up to its attempt budget, then re-scan and continue. Each
  recovery is recorded in `result.recoveries`.
- **hard_failure** → stop and return `{status: failure}` with the step, what was
  expected, what was observed, plus a screenshot and a redacted DOM snapshot for
  debugging.

A checkpoint that fails with *no* matching condition is a hard failure with full
evidence — but first, if enabled, a **bounded, policy-checked assisted recovery**
may fire: exactly one LLM decision, for the failing step only, capped per run
(`--assist`, `max_assists`), fully policy-gated and recorded as an `AssistEvent`.
Never open-ended. All paths are demonstrated in `evidence/` (success, not-found,
permission-denied, interstitial-recovered, session-timeout). Transient slowness
is handled by explicit waits.

**A refusal is not a failure.** The result contract separates things that *went
wrong* from things that were *decided*. A business outcome is a decision by the
application; a **refusal** is a decision by us — a value ceiling, an unmet
dual-control requirement, an unapproved irreversible step, a URL outside the
allowlist. Only `FAILURE` means something actually broke. The distinction is
operational, not cosmetic: the caller's response to `VALUE_LIMIT_EXCEEDED` is to
change the *request* (a smaller amount, a second approver), while its response to
`FILL_NOT_APPLIED` is to investigate the *system*. Typing both as failure would
repeat, one layer up, exactly the mistake that conflating "no such member" with a
crash would be — and the evidence shows the split: scenarios 11–13 are `refused`,
scenario 10 is `failure`. The refusal set is the same set that is never escalated
to a human, because a person at a browser cannot fix a policy file.

**A fill is verified on the control, not the page.** A `FILL` has no page-state
consequence, so no checkpoint can see one that silently failed -- a readonly,
disabled, or JS-masked input accepts the keystrokes, the action reports success,
and the flow runs on empty data. Fill steps therefore carry `verify_value`: read
the control back and assert the write landed (`FILL_NOT_APPLIED`, with evidence).
A `sensitive` value is asserted **non-empty only** -- never compared, never
logged, because reading a credential back to diff it would reintroduce exactly
the leak the observation indexer avoids. Evidence: `replay-10-fill-not-applied`,
against an injected input that accepts a write and discards it.

**Declared outputs are guaranteed.** A run cannot report `success` while an
output the contract promised is missing -- that would be a silent breach, worse
than an error because nothing downstream knows to check. Note the split of
responsibility: a failed extract is deliberately *not* fatal at the step, so the
condition detectors still get their chance to explain *why* the value was absent
(`PERMISSION_DENIED` is a better answer to the caller than "extraction failed");
the contract is then enforced once, at the end of the run, as
`OUTPUT_EXTRACTION_FAILED` with full evidence.

**Locator robustness.** Beyond the ordered candidates, legacy inputs with no
`<label for>` get a **label-proximity** locator (`//tr[td="User ID"]//input` —
"the box next to this text"), ranked above structural paths. Replay records which
candidate resolved and emits a `locator_drift` signal on any fallback, so
**UI/tenant drift** is observable rather than silent; checkpoints failing loudly
absorb the rest. The `StabilitySignal` (`replay --repeat N` → pass rate) and the
`draft→approved` gate turn drift into a health score that catches problems before
unattended production.

## 4. Heterogeneity & multi-tenant

**Other surfaces — demonstrated, not argued.** A second `Surface` is built:
`surface/accessibility.py` perceives only an accessibility tree ({role, name,
value, bounds} nodes) and acts only through a mouse and a keyboard. It shares no
targeting machinery with the Playwright surface — no CSS, no XPath, no
`element.fill()`. The **same artifact**, recorded on the DOM surface, replays
through it to identical outputs with zero drift (evidence 14); business outcomes
and `surfaces_outputs` behave identically. The tree is sourced from Chromium over
CDP rather than an OS API, because there is no desktop application here to drive:
the delta to a real UIA/AX driver is `_ax_nodes()`, one method wide, and the
vocabulary it already returns is the OS vocabulary.

Building it changed the design, which is the point of building it. The legacy
form's inputs have **no accessible name at all**, so they are unaddressable by
name on a DOM *and* on an a11y tree; the only durable handle is the one a person
uses — proximity to the words "User ID". That is now `LocatorKind.NEAR_LABEL`, a
statement of intent the web surface resolves structurally (same table row) and the
a11y surface resolves spatially (nearest control to the label's bounds). An
accessibility tree also stops at a frame boundary, so the driver walks the frame
tree explicitly — the desktop analogue being a nested pane. Each surface declares
`supported_locator_kinds`, and `portability_report` uses that to answer "will this
artifact run there?" **before** anything launches: discovering that step 7 is
unreachable after steps 1–6 have run is how automation half-completes an
irreversible flow.

**Other surfaces.** The seam is `Surface`. The schema speaks roles, names,
labels, and frames — vocabulary a desktop **accessibility tree** exposes just as
a browser does — so the artifact format does not assume a DOM. A legacy-web
target (framesets, nested tables) is the same web surface leaning harder on
`frame_path` and the structural-fallback candidates; the label-relative readout
locators already target exactly the table-soup case. A **desktop** target is a
new `Surface` backed by an OS accessibility API (UIA/AX) or screenshot+coordinates
(the schema already has a `COORDINATES` locator kind and the surface a
coordinate-click path). Perception and action change; the recorded flow, the
compiler, the replay engine, the error contract, and safety do not.

**Multi-tenant reuse.** `Target` splits the shared flow from the per-tenant
binding, so hundreds of tenants on the same vendor product share one artifact and
differ only by `base_url`/`tenant_id`/`version` (plus optional overrides). Two
mechanisms make this real rather than aspirational:

- **Vendor-level condition libraries** (`knowledge.py`): how "Corebank" signals a
  locked account or a maintenance gate is curated once per vendor and inherited
  by every tenant's artifact — one place to maintain, not one per institution. A
  tenant that re-brands the text supplies an override entry.
- **Semantic-first locators**: role/label/text candidates are far more likely to
  survive per-tenant theming and version skew than structural paths, so the same
  artifact degrades gracefully across variants instead of breaking.

This is **built and demonstrated**, not just designed. A second tenant variant
(`MOCKBANK_VARIANT=summit`) ships the same vendor product with rebranded labels
("Member Number" vs. "Member ID", "Find" vs. "Search", "Log In" vs. "Sign On").
`bankcua/tenancy.py` re-binds one artifact to a tenant via a `TenantOverride`
(base_url + a small `label_map`) and `canonicalize_url` normalises routes
(`/member?mid=12345` → `/member?mid=:id`). Evidence shows the member-lookup
artifact, recorded on `demo-cu`, running against `summit-cu`: **with** a ~4-line
override it succeeds cleanly with zero drift; **without** the override it *still*
succeeds via the structural locator fallbacks, emitting drift signals at exactly
the label/role-based steps — graceful degradation, not a cliff.

Per-tenant/version **drift** is detected by watching checkpoint failures and
locator fallbacks firing: replay records *which* candidate resolved and emits a
`locator_drift` signal whenever a non-primary candidate is used (see
`ReplayResult.drifts`). The multi-run **stability signal** (`replay --repeat N`)
turns that into a per-tenant health score that gates unattended use (the
draft→approved workflow). I did not build multi-tenant *plumbing* (a tenant
registry, scheduling) — that is the scaling infrastructure the brief says not to
build prematurely.

## 5. Escalation & handoff

**Detecting "stuck."** In discovery, the loop escalates on no-navigation-progress
over N steps, an unresolvable target, a policy block, or a model `escalate`
call. In replay, the trigger is a step that policy gates
(`NEEDS_CONFIRMATION` for an irreversible action) or an unrecoverable condition.

**Routing with context.** An `InterventionRequest` carries the capability/goal,
the current step, the live URL, a screenshot and DOM snapshot, the reason, a
**control token** (`controller ∈ {agent, operator}`), and the **CDP endpoint** of
the live session. It is written to a file-backed inbox; the automation blocks on
it (and, unattended with nobody home, times out and aborts cleanly with the
request preserved for triage).

**Taking control of the *same* live session.** The browser is launched with a
`--remote-debugging-port`, so the operator attaches to the **same Chromium** via
`connect_over_cdp` and drives the **same page** — cookies, half-filled forms, and
current URL all preserved. This is genuine co-control, not a fresh session. The
operator performs the manual step, each action is **recorded** into the request's
`human_actions`, and on resolve the control token returns to `agent`. Replay then
either resumes automation or, if the human performed the gated step manually,
skips execution and verifies the checkpoint — so an irreversible action is never
double-executed. This full round trip runs in `evidence/escalation-06-handoff`
and via the `operator` CLI (README §4).

**The console.** The control-transfer model was always real; what was mocked was
the human's window onto it — a CLI where the operator typed
`--do click_selector:input[type=submit]`, which is not how a bank operator works.
That window is now built (`escalation/console.py`, `operator console --id ...`):
it serves the live page as a picture, forwards clicks at the coordinates the human
clicked **on that picture**, forwards keystrokes, and records each one into the
intervention before handing control back. No selector appears anywhere, because
the person is not thinking in selectors. One worker thread owns the session and
every request queues work to it — Playwright's sync API is thread-affine, and a
live banking screen is a single-owner resource where interleaved writes are not
debuggable. Human actions are persisted as they happen rather than at resolve, so
a console that dies mid-handoff cannot lose the record of what a person already
did to a live account.

## 6. Safety

Guardrails are enforced on **every action, before it happens, in both discovery
and replay** — a violation raises, it never warns-and-continues.

- **Layered allowlist.** A global policy (`config/policy.yaml`) lists permitted
  URL patterns and action types and an explicit denylist (e.g. `/logout`, the
  test control plane); the artifact's `target.allowed_url_patterns` narrows
  further. A URL must satisfy both. Anything else is blocked (fail closed).
- **Irreversibility, treated conservatively.** Reversible actions (navigate,
  read, fill, select) are `safe`. Create/confirm/submit/delete are `risky` and
  **blocked unless explicitly approved** for the run (`--allow-risky`), or gated
  behind human confirmation when flagged — which, unattended, becomes an
  escalation. In a bank, refusing to create a sub-account is far cheaper than
  creating one by mistake, so failing closed on the irreversible class is the
  right default.
- **Risk classification: heuristic proposes, human ratifies.** Two layered
  signals. *Lexical* — the caption matches create/confirm/submit/delete/transfer;
  cheap, and catches link-styled actions and GET-based mutations. *Structural* —
  the control submits a form and that form uses POST; evidence about what the
  action **does** rather than what it is called, so it survives relabelling,
  translation and per-tenant branding, with a direct analogue on a desktop
  accessibility tree. The structural signal *corroborates* rather than overrides:
  a POST submit is risky unless its caption is on a short, explicit benign list.
  Promoting every POST would flag "Sign On" and "Search", and a guardrail that
  cries wolf gets switched off; what the layering buys over the caption alone is
  the unlabelled mutation — "Apply", "Proceed", "Finalise" carry no risky keyword
  but still change state. The class **and the reason for it** are persisted on the
  step, and `catalog approve` refuses to promote a capability while any risky step
  is unreviewed. A reviewer may also *downgrade* a false positive with a recorded
  justification — a gate that can only rubber-stamp is a checkbox.
- **Value-level (semantic) policy.** The allowlist is URL/action-shaped: it can
  answer "may the agent click here", never "is this amount sane". A third layer
  reads the **inputs** a capability is invoked with, before the browser opens.
  `max` is a hard ceiling — refused outright, never escalated, because no operator
  standing at a browser should wave through an amount the institution has already
  ruled out. `dual_control_above` is the softer band: permitted, but not by one
  person alone; it resolves to an independent second approver, or to an operator
  counter-signature, and unattended it fails closed. A run may not approve itself,
  and an approver must appear in a configured registry — without one,
  `--approver whoever` is theatre. `max_per_window` bounds the **sum** over a
  rolling window against a file-backed ledger, because a per-invocation ceiling is
  blind to velocity: ten $999 deposits inside a minute clear a $1,000 limit ten
  times over. Amounts are booked only on success; a refused or escalated run moved
  no money and must not starve the budget. A governed value that will not parse as
  a number is refused — a limit you cannot evaluate is not a limit. The value gate
  and the irreversible-step gate are independent and compose: evidence 13 clears
  the value gate on a counter-signed $1,500 deposit and is then stopped by the
  step gate on the irreversible click.
- **Never persist secrets or regulated data.** Sensitive inputs are declared in
  the schema and stored as parameter *names*, never values; the artifact records
  `secret_param` references. Redaction is two-layered: name-based (declared
  secrets) and pattern-based (SSN, card, email as a safety net), applied to
  logs, transcripts, and DOM snapshots. Crucially, the observation indexer never
  captures a control's typed value, so secrets never reach the model prompt or
  the logs in the first place. The compiler additionally scrubs run-specific
  values out of the human-readable step intents: the model narrates in prose
  ("the provided operator credentials"), which would otherwise bake one run's
  data -- and potentially a real credential -- into a committed artifact, so
  inputs become `{param}` placeholders and extracted values `<output>` (both the
  raw and the transformed form). Card detection is Luhn-checked so the safety net
  does not fire on ordinary digit runs such as timestamps, and literal matching
  is token-bounded; the mapping form names the parameter
  (`***REDACTED:username***`) so an over-redaction reads as intentional rather
  than as corrupted evidence. Over-redaction remains the deliberate bias. The
  repo-wide sweep of `capabilities/` for secrets is not a claim but an enforced
  invariant (`tests/test_artifact_hygiene.py`), so it cannot silently regress the
  next time a capability is recorded.

**Limits.** Screenshots can still show PII on screen; today they are treated as
sensitive evidence — the production answer is masked capture and a restricted
evidence store. The velocity ledger is a JSON file with an append write: correct
for one process, and the seam (`Ledger`) is where an institution's ledger of
record would go. The approver registry is a config list standing in for a
directory; binding it to authenticated identity changes where the set comes from,
not the shape of the check. The benign-submit list is a curated regex: short and
reviewable by design, but still maintained per vendor.

## 7. Cuts

Most stretch goals are now **built**: agent-facing capability API (`serve`),
code generation (`codegen`), confidence/approval (stability signal + draft→approved
gate), assisted single-step recovery, and cross-tenant reuse with per-variant
overrides and route canonicalisation. What remains deliberately thin, at clean
seams:

- **Operator console** — CLI stand-in; the handoff mechanism is real (§5).
- **Desktop / legacy-web *surfaces*** — designed, not built (§4). The `Surface`
  seam keeps the core uncommitted; cross-tenant reuse *within* the web surface is
  built and demonstrated.
- **Multi-tenant plumbing** (tenant registry, scheduling) — intentionally not
  built; that is the scaling infrastructure the brief says to avoid.
- **Discovery** — the committed evidence is a **genuine live run** through the
  Anthropic Messages API (`AnthropicProvider`), in
  `evidence/discovery-*-anthropic-live/` (`recorded_by: anthropic`). A key-free
  offline reproduction via the `bridge` provider (`scripts/run_discovery.sh`)
  drives the same real loop from a recorded decision trace.

Since the first pass, four of the gaps this section previously named as future
work are **built and evidenced**: value-level policy with amount ceilings and
dual control on money movement (§6, evidence 11–13); a layered risk model with a
per-step human review gate before approval (§6); fill verification, closing the
one action whose effect no page checkpoint could see (§3, evidence 10); and
`surfaces_outputs`, so a business outcome can return the data it does have (§3,
evidence 03).

All four items this section previously listed as future work are now **built and
evidenced**: a second `Surface` (§4, evidence 14); value-level policy with
ceilings, dual control, a velocity ledger and a registry-bound approver (§6,
evidence 11–13); a real co-browsing operator console over the existing CDP seam
(§5); and a drift-driven artifact repair loop (`repair analyse|apply`) that
aggregates drift across runs, proposes a reviewable patch, bumps the version and
lands it in `draft` so the existing approval gate still decides. That loop is
deliberately not self-modifying: automation that silently rewrites its own
instructions for driving a bank is a worse problem than the staleness it fixes.
It also refuses the repairs that would make things worse — drift from a semantic
primary to a structural fallback is a **renamed** control, not a stale locator,
so it reports "supply a tenant label_map" rather than permanently demoting the
step to a CSS path.

**What I'd build next, in order:** (1) a genuine OS-level driver (UIA/AX) behind
the `_ax_nodes()` seam, to replace the one remaining stand-in in the surface
story; (2) binding dual control and the approver registry to authenticated
identity, and moving the velocity ledger to the institution's ledger of record;
(3) a screencast transport for the console in place of frame polling, which is a
bandwidth change rather than a capability one; (4) letting the repair loop propose
tenant `label_map` entries directly by reading what the renamed control now says,
rather than naming the string a human must supply.
