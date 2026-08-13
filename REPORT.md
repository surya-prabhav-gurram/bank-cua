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
interface; nothing speaks Playwright except one file.

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

- **business_outcome** → stop and return `{status: business_outcome, code}`.
  "No such member" and "permission denied" are *answers the caller wants*, not
  errors. (`ReplayStatus.BUSINESS_OUTCOME`, never `FAILURE`.)
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

**Locator robustness.** Beyond the ordered candidates, legacy inputs with no
`<label for>` get a **label-proximity** locator (`//tr[td="User ID"]//input` —
"the box next to this text"), ranked above structural paths. Replay records which
candidate resolved and emits a `locator_drift` signal on any fallback, so
**UI/tenant drift** is observable rather than silent; checkpoints failing loudly
absorb the rest. The `StabilitySignal` (`replay --repeat N` → pass rate) and the
`draft→approved` gate turn drift into a health score that catches problems before
unattended production.

## 4. Heterogeneity & multi-tenant

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

**What is real vs. mocked.** The control-transfer *model* is real: pause, cede
via the token, CDP attach to the live session, recorded human actions, resume,
hand back. The operator *console* is a CLI stand-in for a real-time co-browsing
UI, which is explicitly out of scope; the seam it plugs into is real.

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
- **Never persist secrets or regulated data.** Sensitive inputs are declared in
  the schema and stored as parameter *names*, never values; the artifact records
  `secret_param` references. Redaction is two-layered: name-based (declared
  secrets) and pattern-based (SSN, card, email as a safety net), applied to
  logs, transcripts, and DOM snapshots. Crucially, the observation indexer never
  captures a control's typed value, so secrets never reach the model prompt or
  the logs in the first place. A repo-wide sweep of `evidence/` and
  `capabilities/` finds no secret.

**Limits.** Screenshots can still show PII on screen; today they are treated as
sensitive evidence — the production answer is masked capture and a restricted
evidence store. The allowlist is URL/action-shaped, not semantic (it can't tell
"transfer $1" from "transfer $1M"); value-level policy (limits, dual control on
amounts) is the next layer. Risk classification is keyword+action heuristic and
should become an explicit per-step review gate before approval.

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

**What I'd build next, in order:** (1) a second `Surface` (desktop accessibility
tree) to prove the seam against a genuinely non-DOM target; (2) value-level
(semantic) policy for amounts and dual control on money movement; (3) a real
co-browsing operator console over the existing CDP seam; (4) an artifact
auto-repair loop that proposes locator/override updates when drift crosses a
threshold, gated by the approval workflow.
