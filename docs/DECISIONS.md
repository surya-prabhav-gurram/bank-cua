# Decision Record

Why this system works the way it does.

This is the companion to [REPORT.md](../REPORT.md). REPORT.md is the narrative a
reviewer reads top to bottom, and it is constrained to the seven headings the
brief mandates. This file is the underlying ledger: every fork where a reasonable
engineer could have chosen differently, what the alternatives were, and what we
gave up by not taking them.

**Context that sets the bar for every entry below.** This system gives an AI agent
hands inside a bank's back-office software, operating on real member accounts and
regulated data. Two consequences run through the whole record:

- **Failing closed beats failing useful.** A refused action costs a retry. A
  wrongly-permitted irreversible action costs a member's account. Where an entry
  looks over-cautious, that asymmetry is why.
- **A control that cannot be checked is not a control.** Several entries below
  exist because a guardrail was present in configuration but absent in the
  execution path, or a claim was made in prose that no test enforced. Anything
  asserted here should be verifiable in the repo, and most entries name the test.

## How to read an entry

Every decision carries the same five fields:

| Field | What it holds |
|---|---|
| **Fork** | What is being decided, in one line |
| **Options** | Every credible alternative, including the ones rejected |
| **Chosen** | What we did |
| **Why / cost** | The reasoning, and what we are knowingly giving up |
| **Revisit when** | The condition that would make this choice wrong |

That last field is the one that matters most in review. "We chose X" is a claim;
"we chose X, and here is what would have to become true for X to be wrong" is a
position that can be argued with.

## Status legend

| Status | Meaning |
|---|---|
| **Locked** | Load-bearing. Changing it changes the system's shape. |
| **Settled** | Decided and stable, but cheap to revisit. |
| **Provisional** | Correct for the current scope; explicitly expected to change for production. |
| **Open** | Not yet decided. Options laid out, awaiting requirements. |

## Format tiers

Decisions where the alternatives were genuinely live get a **full entry**.
Smaller choices — real decisions, but ones where the alternative was clearly worse
— are recorded in the **compact register** at the end of each section, one row
each, still naming the alternative and the reason. Nothing is omitted; the tiering
is about proportion, not completeness.

## Index

| Section | Range | Subject |
|---|---|---|
| [1. Architecture](#1-architecture) | ARCH-01 … ARCH-09 | Runtime, dependencies, process model, boundaries |
| [2. Artifact schema](#2-artifact-schema) | SCHEMA-01 … SCHEMA-14 | The capability contract |
| [3. Determinism & error handling](#3-determinism--error-handling) | REPLAY-01 … REPLAY-12 | The replay contract and the error taxonomy |
| [4. Surfaces & heterogeneity](#4-surfaces--heterogeneity) | SURF-01 … SURF-07 | The perceive/act seam and the second surface |
| [5. Multi-tenant reuse](#5-multi-tenant-reuse) | TENANT-01 … TENANT-06 | One artifact, many institutions |
| [6. Escalation & handoff](#6-escalation--handoff) | ESC-01 … ESC-09 | Control transfer on a live session |
| [7. Safety](#7-safety) | SAFE-01 … SAFE-14 | Guardrails, risk, value policy, redaction |
| [8. Observability](#8-observability) | OBS-01 … OBS-05 | Evidence and its integrity |
| [9. Agent-facing surface](#9-agent-facing-surface) | AGENT-01 … AGENT-05 | Catalog, API, codegen |
| [10. Corrections](#10-corrections-post-review) | FIX-01 … FIX-08 | Claims the round-1 code did not enforce |
| [11. Adaptation to MERIDIAN](#11-adaptation-to-meridian-core) | MER-01 … MER-21 | Round-2: the live target, the API, chatbot and dashboard |
| [12. Open decisions](#12-open-decisions-awaiting-requirements) | OPEN-01 … OPEN-07 | Analysed, deliberately not built |
| [13. Live-model findings](#13-live-model-findings) | LIVE-01 … LIVE-10 | Defects only a real model against the real target could surface |
| [14. Sign-in](#auth-01--the-session-token-carries-an-identity-never-a-permission--locked) | AUTH-01 … AUTH-22, DC-01 | Who is signed in, what that permits, and the sign-on at the door |

---

# 1. Architecture

## ARCH-01 — Python as the implementation language · **Locked**

**Fork.** What language carries the agent loop, the replay engine, and the surface drivers.

**Options.**
- **Python.** Best-in-class LLM SDKs, Playwright sync API, Pydantic for schema validation. Weakest concurrency story of the three.
- **TypeScript/Node.** Playwright's native home; one language shared with any future operator-console front end. Runtime validation (zod) is decent but weaker than Pydantic at JSON-schema generation.
- **Go/Java.** What a bank's platform team likely runs in production. Strong concurrency and deployment story, far worse ergonomics for LLM tool-use and browser automation.

**Chosen.** Python.

**Why / cost.** The load-bearing artifact in this system is a *typed schema* that must serialise to reviewable JSON, validate on load, and generate a function-calling manifest for a calling agent. Pydantic v2 does all three natively, and `schema.py` is explicitly a focal point of the evaluation. The cost is real: the concurrency model (ARCH-03) is constrained by it, and a bank's platform team may eventually want this in a JVM. That is a rewrite of the drivers, not of the schema — which is the point of the seams.

**Revisit when.** Throughput requirements arrive that a single synchronous process cannot meet, *and* the bottleneck is proven to be the runtime rather than the browser.

## ARCH-02 — Playwright as the browser driver · **Locked**

**Fork.** How we actually perceive and act on a web surface.

**Options.**
- **Playwright.** Role/accessibility locator model, robust auto-waiting, first-class frame handling, and a CDP endpoint we can reuse for the human handoff.
- **Selenium.** Ubiquitous in enterprise, widest browser support. Weaker waiting primitives, no built-in accessibility locators, clumsier frame handling.
- **Puppeteer.** Good CDP access, Chromium-only, weaker locator vocabulary than Playwright.
- **A hosted CUA/agent SDK.** Fastest to a demo. Hides exactly the mechanism being evaluated — locator strategy and control transfer — behind a vendor's abstraction.

**Chosen.** Playwright (sync API).

**Why / cost.** Three properties made it the only real candidate. Its locator model is *role + accessible name*, which is the same vocabulary a desktop accessibility tree exposes — so the artifact format that falls out of it ports conceptually to a desktop surface instead of hard-coding a DOM (see SURF-01). Its frame handling makes the iframe'd balance pane addressable. And it exposes a CDP endpoint, which is what makes same-session human handoff real rather than a fresh-session fake (ESC-01). The cost is a Chromium dependency and the sync API's thread-affinity, which forces the console's worker-thread design (ESC-07).

**Revisit when.** A target surface is a native desktop app — at which point Playwright is not replaced but *joined* by a UIA/AX driver behind the same `Surface` interface.

## ARCH-03 — Single synchronous process, file-backed state · **Locked**

**Fork.** Process and persistence model.

**Options.**
- **Single process, files.** No queue, no database, no services. Everything is a JSON file on disk.
- **Services + queue.** A discovery worker, a replay worker, a broker. Matches how this would really be deployed at a bank.
- **Single process, embedded DB (SQLite).** Middle ground; gains transactions and concurrent-write safety.

**Chosen.** Single process, file-backed.

**Why / cost.** The brief is explicit that building scaling infrastructure is *not* rewarded and that designing abstractions which *could* scale is. The parts that would actually need to scale — the artifact, the catalog, the handoff inbox, the ledgers — are already serialisable, stateless and addressable by id, so promoting any of them to a service is a storage swap rather than a redesign. The cost is concrete and lands in two places: the velocity ledger has no write locking (SAFE-11, OPEN-05), and two concurrent replays of a money-moving capability could interleave against it. That is acceptable at this scope and explicitly not acceptable in production.

**Revisit when.** More than one replay can run concurrently against the same value-policy budget — which is the moment the ledger needs real transactions, not a bigger file.

## ARCH-04 — One dependency direction: everything speaks schema + Surface · **Locked**

**Fork.** How the modules are allowed to depend on each other.

**Options.**
- **Layered with a strict direction.** The schema and the `Surface` interface are the only things the engines import; concrete drivers are leaves.
- **Pragmatic/flat.** Let the replay engine import Playwright directly where convenient.

**Chosen.** Strict direction. Only `surface/web_playwright.py`, `surface/accessibility.py` and the operator's CDP client import Playwright.

**Why / cost.** This is the claim §3.7 asks us to make credible: that reaching a desktop surface does not touch the schema, the compiler, the replay engine, the error taxonomy or the safety model. A single `import playwright` inside the engine would silently falsify it. The cost is occasional indirection — the engine asks the surface `check(checkpoint)` rather than reaching for a page object. Building the second surface (SURF-02) is what proved the direction held, and it found one real leak: the engine's use of `hasattr(surface, "wait_for_selector")`, an honest capability probe rather than a type assumption.

**Revisit when.** Never, while heterogeneity is a requirement. This is the seam the whole §4 story rests on.

## ARCH-05 — Discovery and replay share the same targeting primitives · **Locked**

**Fork.** Does the discovery loop act through the same Locator machinery replay uses, or through its own faster path?

**Options.**
- **Shared.** Discovery synthesises a `Locator` from the element the model picked and acts *through* it.
- **Separate.** Discovery acts directly on the live element handle it already has; the compiler derives locators afterwards.

**Chosen.** Shared.

**Why / cost.** The separate path is faster and simpler, and it produces locators nobody has ever executed — the artifact would record a *guess* about how to find the control, validated for the first time on the first production replay. Sharing means the exact targeting that gets recorded is the exact targeting that already worked once. The cost is that discovery pays locator-resolution overhead it does not strictly need.

**Revisit when.** Never. This is what makes recorded locators trustworthy rather than hopeful.

## ARCH-06 — The model addresses elements by integer ref, never by selector · **Locked**

**Fork.** How the LLM tells the system which control to act on.

**Options.**
- **Integer `ref`** into an enumerated observation; the system synthesises the locator.
- **The model writes selectors** (CSS/XPath) directly.
- **Coordinates** from a screenshot.

**Chosen.** Integer ref.

**Why / cost.** If the model writes selectors, locator quality becomes a property of prompt luck, and it varies run to run. With refs, the model does perception and intent — the part it is good at — and locator robustness becomes a property of code that can be tested (`test_surface.py`, `test_locator_vocabulary.py`). It also shrinks the model's action space, which reduces the ways a discovery run can go wrong. The cost is that the system can only target what the indexer enumerated; a control the indexer misses is invisible to the model regardless of how clearly it appears on screen.

**Revisit when.** A surface appears where enumeration is impossible and screenshot+coordinates is the only perception available — the schema already carries `COORDINATES` for that floor.

## ARCH-07 — The compiler is the record→capability boundary · **Locked**

**Fork.** Is the artifact the transcript, or something derived from it?

**Options.**
- **Separate compiler.** Transcript is evidence of *how it was discovered*; artifact is a *contract*.
- **Artifact is the transcript**, lightly cleaned.
- **Artifact embeds the transcript** for traceability.

**Chosen.** Separate compiler; transcript referenced by path + hash, never inlined.

**Why / cost.** These are two different objects with two different audiences. A calling agent needs a stable typed contract; an auditor needs the raw run. Fusing them means every model verbosity change churns the capability, and the artifact inherits whatever the model happened to say — including, as we found, run-specific values and echoed credentials in step intents (SAFE-13). Keeping them separate lets the compiler *scrub*. The cost is a translation layer that can lose information, and a provenance link that can rot — which it did (FIX-08).

**Revisit when.** Never. This is the record-once/replay-many boundary.

## Compact register — architecture

| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| ARCH-08 | Pydantic v2 for all schema types | Dataclasses + hand-rolled validation; `attrs` | Validation on load, JSON round-trip, and function-calling manifest generation all come free; the artifact is the evaluation's focal point and needs to validate, not just parse |
| ARCH-09 | Flask for the mock app, operator console, and capability API | FastAPI; raw `http.server` | Three small servers, none performance-sensitive; FastAPI's async model would fight Playwright's thread-affine sync API in the console (ESC-07). Boring is correct here |

---

# 2. Artifact schema

## SCHEMA-01 — The artifact is a contract, not a step list · **Locked**

**Fork.** What shape is the thing a successful discovery run produces?

**Options.**
- **Contract.** Typed `inputs`, typed `outputs`, a `success` condition, and description — understandable without reading the steps.
- **Step list.** An ordered sequence of actions; the caller infers the interface.
- **Executable script.** Emit code directly (this exists, but as an *export* — see AGENT-04).

**Chosen.** Contract, with steps as an implementation detail underneath it.

**Why / cost.** Two audiences have to understand this object without executing it: an AI agent deciding whether to call it, and a human deciding whether to approve it. A step list serves neither — the agent cannot tell what it returns, and the reviewer cannot tell what it *means* without simulating twelve clicks in their head. Making the contract primary is what lets `catalog manifest` emit a function-calling schema (AGENT-01) and what lets the approval gate be a meaningful human act (SAFE-06). The cost is duplication: `inputs` must stay consistent with what the steps actually reference, and nothing structurally enforces that.

**Revisit when.** A capability appears whose outputs are not knowable until run time. The schema would need an open output map, and the "typed contract" claim would weaken.

## SCHEMA-02 — Every control carries an ordered list of locator candidates · **Locked**

**Fork.** How is a control addressed in the artifact?

**Options.**
- **Ordered candidate list**, most-stable-first, each with reasoning; replay tries them in order.
- **Single best locator.** Simplest, smallest artifact, easiest to review.
- **Single locator + runtime healing.** Re-find the control with an LLM when it fails.

**Chosen.** Ordered candidate list.

**Why / cost.** A single locator makes every step a single point of failure against a surface we do not control. Runtime healing puts a model back in the production decision loop, which is the exact thing this system exists to remove. The candidate list gets robustness *without* the model: one strategy going stale degrades to a fallback rather than a failure, and — critically — the system can tell you it happened, because it records *which* candidate resolved. That signal is the input to drift detection (REPLAY-08) and the repair loop (TENANT-05). The cost is a larger artifact and a subtle failure mode: a step can silently run on a fallback for months, which is why the drift signal is not optional decoration.

**Revisit when.** Never — but note the cost is real, and FIX-01 is an instance of it biting: a primary candidate that never resolved on its own surface looked healthy because the fallback worked.

## SCHEMA-03 — `NEAR_LABEL` as a first-class locator kind · **Locked**

**Fork.** How do you address a legacy form control that has no accessible name at all — no `<label for>`, no `aria-label`?

**Options.**
- **A semantic `NEAR_LABEL` kind** meaning "the thing adjacent to this label text", resolved per surface.
- **XPath only.** `//tr[td="User ID"]//input` — expresses the same idea, bound to a DOM.
- **Positional/coordinate targeting.**

**Chosen.** `NEAR_LABEL`, with the DOM-bound XPath kept as the *second* candidate.

**Why / cost.** This decision was forced by building the second surface, and it is the clearest example of implementation changing design. The legacy login form's inputs are unaddressable by name on a DOM *and* on an accessibility tree — the only durable handle is the one a human uses: proximity to the words "User ID". Expressed as XPath, that intent is welded to a DOM and the step becomes unportable. Expressed as `NEAR_LABEL`, it is a statement of intent each surface resolves in its own terms: structurally on the web (same table row), spatially on the a11y tree (nearest control to the label's bounds). The cost is that "adjacent" is genuinely ambiguous, and each surface's resolution is a heuristic that can pick the wrong neighbour.

**Revisit when.** A layout appears where row-adjacency and spatial-adjacency disagree about which control a label belongs to. Today both mock tenants are table-shaped, which is the legacy norm but not a law.

## SCHEMA-04 — Locator preference order is semantic → structural · **Locked**

**Fork.** Which strategy leads the candidate list?

**Options.**
- **Semantic first** (role+name, label, proximity, text), structural last (CSS, XPath).
- **Structural first.** More precise on the recorded page — an exact path matches exactly one element.
- **Whatever resolved fastest at record time.**

**Chosen.** Semantic first.

**Why / cost.** Structural paths are more precise and *less durable*, and the environment tells us which to weight: the same vendor product is rebranded, retitled and reskinned per tenant, so a CSS path breaks on a theme change while role+name survives it. Semantic strategies are also the only ones a non-DOM surface can honour at all, so leading with them is what makes portability possible rather than accidental (SURF-03). The cost is occasional ambiguity — two buttons named "Search" — which the candidate list absorbs by falling through to a structural path.

**Revisit when.** A tenant is found whose semantics churn faster than its structure. The repair loop already refuses to invert this ordering automatically (TENANT-06), because that inversion is usually a symptom of a *rename*, not of a bad ordering.

## SCHEMA-05 — The error taxonomy lives in the artifact as data · **Locked**

**Fork.** Where does "how this app signals a locked account" live?

**Options.**
- **Declarative `KnownCondition` in the artifact** — detector, class, optional recovery.
- **Hard-coded in the replay engine.**
- **Inferred at runtime by an LLM.**

**Chosen.** Declarative data, sourced from a per-vendor library (TENANT-03).

**Why / cost.** The three-way classification (REPLAY-01) is the single most consequential behaviour in the system — it decides whether the caller is told "no such member", "we retried and continued", or "page an engineer". Burying that in engine code makes it unreviewable by the people who actually know the vendor's UI, and unchangeable without a code deploy. As data it is inspectable in the artifact, curatable per vendor, and overridable per tenant. The cost is expressiveness: detectors are `text_present` / `url_matches` / `http_status` / `element_visible`, so a condition signalled only by, say, a colour change is currently inexpressible.

**Revisit when.** A vendor signals a condition in a way no declarative detector can capture.

## SCHEMA-06 — `Target` separates the shared flow from the per-tenant binding · **Locked**

**Fork.** How does one artifact serve many institutions running the same vendor product?

**Options.**
- **Split.** Steps and locators are shared; `base_url`, `tenant_id`, `vendor_product`, `version` are a swappable binding.
- **Per-tenant artifacts.** Re-record for each institution.
- **Fully parameterised single artifact** with tenant as an input parameter.

**Chosen.** Split binding.

**Why / cost.** Hundreds of tenants × ~20 apps means re-recording per tenant is thousands of recordings and thousands of review burdens — the cost scales with the wrong number. The split makes re-pointing a *binding* change. Making tenant an ordinary input parameter would have been simpler still, but it puts tenant identity in the same namespace as member IDs and deposit amounts, where the value policy and the allowlist would have to reason about it. Keeping it in `Target` keeps "which institution" architecturally distinct from "what the caller asked for". The cost is that the split is only as good as the locators' tenant-neutrality (SCHEMA-04).

**Revisit when.** Tenants of the same vendor product diverge structurally, not just cosmetically — at which point overrides stop being a label map and become a fork.

## SCHEMA-07 — Sensitive inputs are stored as names, never values · **Locked**

**Fork.** How do credentials and PII relate to the artifact?

**Options.**
- **Declare `sensitive: true` on the input; the artifact records a `secret_param` *reference*.** Values supplied only at invocation.
- **Encrypt values into the artifact.**
- **Reference an external secret store from the artifact.**

**Chosen.** Names only, values at invocation.

**Why / cost.** The artifact is a reviewable file that gets committed, diffed, and passed around — the safest secret is one that was never written there in any form, encrypted or not. Encryption would put key management into the artifact's lifecycle for no benefit at this scope. A secret-store reference is the right production answer and is compatible with this decision (it changes *where invocation gets the value*, not the artifact). The cost is that the invocation path must now carry secrets, and today it carries them on the command line — which is a real leak we have not closed (OPEN-02).

**Revisit when.** Immediately, for the ingestion half. The artifact half is settled.

## SCHEMA-08 — Risk class, the *reason* for it, and a human review flag all live on the step · **Locked**

**Fork.** How is irreversibility recorded?

**Options.**
- **All three fields:** `risk`, `risk_reason`, `risk_reviewed`.
- **Just `risk`.** A boolean-ish classification.
- **`risk` + `risk_reviewed`,** without the reason.

**Chosen.** All three.

**Why / cost.** Risk classification is a heuristic (SAFE-04), and a heuristic that cannot show its work cannot be audited — a reviewer facing a bare "risky" can only defer to it or override it blindly. Persisting *why* ("lexical: caption contains 'Confirm'; corroborated by POST form submission") lets a person judge the classification instead of trusting it, and lets them downgrade a false positive with a recorded justification. `risk_reviewed` is what the approval gate keys on (SAFE-06). The cost is three fields where one would do, and a review burden that scales with risky steps.

**Revisit when.** Never. The heuristic-proposes/human-ratifies split is the core of the safety story.

## SCHEMA-09 — `verify_value` on fills · **Locked**

**Fork.** How do you know a fill actually landed?

**Options.**
- **Read the control back** and assert the value is there.
- **Trust the action result.** Playwright reported success.
- **Page-state checkpoint,** as with every other action.

**Chosen.** Read-back, opt-in per step, enabled by the compiler for all fills.

**Why / cost.** This closes the one blind spot no other mechanism can see. A fill's effect is on the *control*, not the page — so a page-state checkpoint cannot observe it, and a legacy input-masking handler that accepts the keystrokes and discards them leaves both the action result and the page looking entirely normal. The flow then runs on empty data all the way to a confirmation screen. Secrets are asserted **non-empty only**, never compared: we already refuse to let a credential reach a log, and reading one back to diff it would reintroduce exactly that leak for a stricter assertion nobody needs. The cost is an extra read per fill.

**Revisit when.** Never. Evidence 10 is this decision earning its place.

## Compact register — artifact schema

| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| SCHEMA-10 | `frame_path` is first-class on every Locator and Checkpoint | Global frame switching; CSS `>>>` piercing | Legacy apps put real content in iframes (the balance pane). Addressing must survive frames, and a desktop a11y tree has the same nesting problem — the vocabulary ports |
| SCHEMA-11 | `ValueType` enum with `MONEY` normalised to cents | Free-form strings; floats for money | A balance returned as `"$4,213.55"` forces every caller to parse it, and float cents drift. Typed outputs are the contract's whole point |
| SCHEMA-12 | Semantic `version` + `approval_state` on the artifact | Content hash as version; no approval concept | A reviewer approves a *version*; the repair loop bumps it and lands in `draft` so the gate re-applies (TENANT-05) |
| SCHEMA-13 | `surfaces_outputs` declared per condition | Always try to extract on a business outcome; never try | "Permission denied" still names the member (the caller needs it to route the request); "not found" has nothing to give. Which is which is a property of the vendor's UI, so it is declared, not guessed |
| SCHEMA-14 | `schema_version` constant on every artifact | Implicit versioning | Artifacts outlive the code that wrote them; a stored contract with no version is unmigratable |

---

# 3. Determinism & error handling

## REPLAY-01 — Three-way classification of every runtime condition · **Locked**

**Fork.** When something unexpected appears on screen, what is it?

**Options.**
- **Three classes.** `business_outcome` (a legitimate answer), `recoverable` (handle in-band and continue), `hard_failure` (stop, surface a debuggable error).
- **Two classes.** Success or failure.
- **Exception types,** letting the caller catch what it cares about.

**Chosen.** Three classes, declared per condition, first match wins.

**Why / cost.** The glossary calls conflating a business outcome with a crash "the most common design mistake here", and the reason is operational: a caller that cannot distinguish "this member does not exist" from "the automation broke" will either page an engineer for a routine answer or silently swallow a real fault. The three classes map onto three genuinely different caller responses — *tell the user*, *nothing to do*, *investigate*. Exceptions were rejected because the classification must be **data in the artifact** (SCHEMA-05), reviewable by people who know the vendor's UI, not a code path. The cost is that classification is only as good as the curated library, and an unclassified condition falls through to a generic checkpoint failure.

**Revisit when.** Never. This is the requirement §3.3 is built around.

## REPLAY-02 — `REFUSED` as a status distinct from `FAILURE` · **Locked**

**Fork.** When a guardrail declines to act, what does the caller see?

**Options.**
- **A distinct `REFUSED` status** carrying `{code, requirement, reason}`.
- **`FAILURE`** with a policy error code.
- **An exception** raised out of the engine.

**Chosen.** Distinct status, applied at one boundary (`_as_refusal`) rather than at each guardrail.

**Why / cost.** This is the same distinction REPLAY-01 makes, one layer up. A refusal means *nothing broke* — the caller's move is to change the **request** (a smaller amount, a second approver, an approved capability), whereas a failure's move is to investigate the **system**. Collapsing them leaves the caller unable to tell "get a bigger mandate" from "page an engineer" at 3am. The `requirement` field states what would have to be true for it to proceed, which makes the refusal actionable rather than merely negative. Doing the retyping at one boundary means exactly one place decides what counts as a refusal — and that same set decides what must never wake a human (ESC-05). The cost is a fourth and fifth status (`ESCALATED` too) for callers to branch on.

**Revisit when.** Never. Over HTTP this is the 403-vs-422 distinction (AGENT-02).

## REPLAY-03 — Per-step checkpoints, asserted after every action · **Locked**

**Fork.** How do we know a step worked before moving to the next one?

**Options.**
- **Assert a declared checkpoint after each step.**
- **Trust the driver's action result.**
- **Assert only the final success condition.**

**Chosen.** Per-step checkpoints, synthesised by the compiler from the observed next state.

**Why / cost.** "Assume the click worked" is how automation half-completes an irreversible flow and then acts on a screen it has misidentified — in a banking context, that is the failure mode with an actual dollar cost. Asserting only at the end tells you *that* something went wrong but not *where*, which makes the failure undebuggable. Per-step checkpoints localise it: the result names the step, what was expected, and what was observed. The cost is more assertions to maintain and a class of false failures when a checkpoint is stricter than reality.

**Revisit when.** Never.

## REPLAY-04 — Declared outputs are guaranteed, enforced once at the end · **Locked**

**Fork.** Can a run report success while an output the artifact promised is missing?

**Options.**
- **No.** Missing declared outputs are a hard failure (`OUTPUT_EXTRACTION_FAILED`), checked once after all steps.
- **Yes,** return partial outputs and let the caller check.
- **Fail immediately** at the extract step that came up empty.

**Chosen.** No — but enforced at the end of the run, not at the failing step.

**Why / cost.** Returning success with a missing output is a *silent* contract breach, which is worse than an error because nothing downstream knows to check. The subtlety is *where* to enforce it. Failing at the extract step would pre-empt the condition detectors and lose the explanation — the reason the balance was unreadable is often `PERMISSION_DENIED`, which is a business outcome the caller needs, not an extraction bug. So a failed extract is deliberately non-fatal at the step, letting the detectors have their say, and the contract is enforced once at the end. The failure then blames the step that was *supposed* to produce the first missing output, so it points somewhere debuggable rather than "somewhere in the run".

**Revisit when.** Never. The split of responsibility here is deliberate and load-bearing.

## REPLAY-05 — Bounded recovery with an explicit attempt budget · **Settled**

**Fork.** How hard does replay try to recover from a recoverable condition?

**Options.**
- **Declared budget per condition** (`max_attempts`, `backoff_ms`), then give up and fail.
- **Retry until a global timeout.**
- **Single attempt.**

**Chosen.** Declared per-condition budget; success is defined as *the condition's signature is gone*, then re-scan for anything the dismissal revealed.

**Why / cost.** Unbounded retry against a bank's system is indistinguishable from hammering it, and a run that will fail should fail promptly so the session is released. Defining success as "the detector no longer fires" rather than "we performed the recovery action" is what stops a recovery from reporting success while the interstitial is still on screen. The re-scan matters because dismissing one gate frequently reveals another. The cost is that a genuinely slow condition can exhaust its budget and be reported as a recovery failure.

**Revisit when.** A vendor is found with legitimately long interstitials; the budget is per-condition data, so this is a tuning change, not a code change.

## REPLAY-06 — Explicit waits, never sleeps · **Settled**

**Fork.** How does replay handle timing?

**Options.**
- **Declared `WaitSpec` per step** — selector, url, load, network_idle, timeout.
- **Fixed sleeps.**
- **Rely entirely on the driver's auto-waiting.**

**Chosen.** Declared waits, honoured by the engine, on top of Playwright's auto-waiting.

**Why / cost.** Sleeps are simultaneously too slow on the happy path and too short under load, and they make replay timing-dependent — the opposite of deterministic. Making the wait *declared data* also means the strategy is reviewable rather than buried. The cost: `load` is effectively a no-op because navigation already waited, so the field is partly decorative for navigations, and `network_idle` is capped at 500ms to avoid pathological waits on pages that poll.

**Revisit when.** A surface appears where the driver has no auto-waiting to build on — a desktop driver, most likely, where waits become the primary mechanism rather than a supplement.

## REPLAY-07 — Assisted recovery is opt-in, single-step and policy-checked · **Settled**

**Fork.** Should an LLM ever re-enter the replay loop?

**Options.**
- **Bounded assist.** Opt-in flag, hard cap (default 1), one action, only for the failing step, and the proposed action goes through the same policy gate as everything else.
- **Never.** Determinism is the product.
- **Open-ended healing.** Let the model recover however it can.

**Chosen.** Bounded assist, off by default.

**Why / cost.** Open-ended healing reintroduces exactly the non-determinism this system exists to remove, and does it at the least supervised moment. But refusing any assist means a single stale locator fails a capability that a human could see is trivially recoverable. The bound is what makes it defensible: one action, one step, policy-checked, recorded as an `AssistEvent` in the result, and never on by default. The cost is a genuine and permanent tension — an assisted run is not a deterministic run, and the result contract has to say so.

**Revisit when.** Assist events show up frequently in production. That is not a signal to loosen the bound; it is a signal that the artifact needs repair (TENANT-05).

## Compact register — determinism & error handling

| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| REPLAY-08 | Record a `DriftSignal` whenever a non-primary candidate resolves | Silent fallback | A step running on a fallback for months is a failure waiting to happen; the signal is the early warning and the input to repair. (Extract steps were excluded by a bug — see FIX-06) |
| REPLAY-09 | First matching condition wins, in declared order | Score all matches and pick the best | Deterministic and reviewable: the order is visible in the artifact. Scoring would make the outcome depend on tie-breaks nobody can see |
| REPLAY-10 | Value transforms (`money_to_cents`, `digits_only`, `strip`) applied at extract | Return raw strings; transform in the caller | The typed output contract is the point; `"$4,213.55"` → `421355` belongs to the capability, not to every caller |
| REPLAY-11 | Params substituted into URLs, values *and* checkpoints at run time | Substitute into actions only | A checkpoint asserting `/member?mid={member_id}` has to track the supplied input, or it asserts the recording's member forever |
| REPLAY-12 | Value-policy evaluation runs before the browser opens | Check at the step that uses the value | The cheapest place to refuse a $1M transfer is before anything has been opened or typed; `steps_executed: 0` is itself evidence |

---

# 4. Surfaces & heterogeneity

## SURF-01 — `Surface` is the seam between perceiving/acting and the recorded flow · **Locked**

**Fork.** Where is the boundary that makes a desktop or legacy-web target reachable without redesigning the system?

**Options.**
- **An abstract `Surface`** exposing perception (`observe`, `index_elements`, `index_readouts`) and Locator-based actions (`click/fill/select/read/check/detect`).
- **Driver-per-target with no shared interface.**
- **A lowest-common-denominator interface** — screenshot in, mouse/keyboard out.

**Chosen.** Abstract `Surface`, with the schema and both engines importing only it.

**Why / cost.** The screenshot-only interface is genuinely portable and throws away everything that makes replay reliable: no roles, no names, no structural fallbacks, no way to read a value except OCR. The richer interface bets that every surface worth automating exposes *some* queryable tree — which is true for browsers (DOM/AX) and for desktop apps (UIA/AX), and it is what lets one artifact serve both. The cost is that the interface has ten methods a new driver must implement, and two of them (`index_readouts`, `locator_for_element`) carry real judgement rather than mechanical translation.

**Revisit when.** A surface appears with no queryable tree at all. `COORDINATES` is the schema's floor for that case, but a flow built entirely on it would be brittle enough to question the whole approach.

## SURF-02 — Build a second surface rather than argue the seam holds · **Locked**

**Fork.** §3.7 explicitly says heterogeneity may be *designed*, not built. So: design it, or build it?

**Options.**
- **Build a real second `Surface`** that shares no targeting machinery with the first.
- **Design only,** as the brief permits.
- **Build a thin variant** of the web surface (e.g. frames-only legacy mode) — cheaper, but shares all the machinery.

**Chosen.** Build `AccessibilitySurface`: perception is a tree of `{role, name, value, bounds}` nodes, action is a mouse click and keystrokes. No CSS, no XPath, no `element.fill()`, no DOM query anywhere.

**Why / cost.** The claim "the schema does not assume a DOM" is unfalsifiable until something without a DOM runs the same artifact. Building it turned that from an argument into evidence (scenario 14) — and it immediately paid for itself by *changing the design*: it exposed that role+name alone could not address the login form, which is why `NEAR_LABEL` exists (SCHEMA-03). A thin variant would have shared the machinery and proved nothing. The cost is a second driver to maintain, and one honest stand-in: the tree comes from Chromium over CDP rather than an OS API (SURF-04).

**Revisit when.** Never. This is the difference between §4 being credible and being a promise.

## SURF-03 — Surfaces declare `supported_locator_kinds`; portability is decided statically · **Locked**

**Fork.** How do you know whether an artifact can run on a given surface?

**Options.**
- **Declare capability as data** per surface; compute a static `portability_report` before launching.
- **Try it and see** — attempt the run, fail when a step is unreachable.
- **Runtime capability probing** at each step.

**Chosen.** Declared data + static report.

**Why / cost.** "Try it and see" is the dangerous option in this domain, and specifically because of irreversibility: discovering that step 7 is unreachable *after* steps 1–6 have run is how automation half-completes a sub-account creation. Asking before launching costs nothing and answers definitively, because every step already carries an ordered candidate list — a step is portable iff at least one candidate uses a strategy the surface declares. The cost is that a declared capability can drift from actual behaviour; a surface could claim `NEAR_LABEL` support and resolve it badly.

**Revisit when.** That drift shows up — which it did, in a form the static report could not see (FIX-01): the web surface *declared* `NEAR_LABEL` support and resolved it only for form controls, silently falling through on read-only values.

## SURF-04 — The a11y tree comes from CDP, and the seam is one method wide · **Provisional**

**Fork.** Where does the accessibility tree come from, given there is no desktop app in this project?

**Options.**
- **Chromium over CDP** (`Accessibility.getFullAXTree`), isolated behind a single `_ax_nodes()` method.
- **A real OS API** (Windows UIA / macOS AX) driving a real desktop app.
- **Skip the second surface** and design only.

**Chosen.** CDP-sourced, with the substitution boundary deliberately one method wide.

**Why / cost.** There is no desktop application in scope to drive, and inventing one would have spent the budget on a fixture rather than on the seam. What matters is the *vocabulary* the method returns — `{role, name, value, bounds}` — which is already the OS vocabulary, so a UIA/AX driver swaps the body of `_ax_nodes()` and nothing else. This is stated plainly as a stand-in in REPORT §4 and §7 rather than presented as a desktop driver. The cost is that the last mile is unproven: real AX trees are messier, slower, and have quirks a Chromium-derived tree does not.

**Revisit when.** Round 2 or later, when a real desktop target exists. This is the first item on the "what I'd build next" list.

## Compact register — surfaces

| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| SURF-05 | The a11y surface walks the frame tree explicitly | Assume one flat tree | An accessibility tree stops at a frame boundary, so a frameset legacy app would be invisible. The desktop analogue is identical — a UIA driver walks nested panes |
| SURF-06 | Unsupported candidates are *skipped*, not failed, during resolution | Fail the step on an unhonourable candidate | An artifact recorded on the web carries CSS/XPath that is meaningless on an a11y tree; skipping is what lets one recording serve both surfaces |
| SURF-07 | The a11y surface honours a by-value select as by-label, and says so in the result | Silently do by-label; fail the step | An a11y tree exposes option labels, never underlying values — and a desktop combobox has the same property. Reporting the substitution beats pretending |

---

# 5. Multi-tenant reuse

## TENANT-01 — One artifact plus a small override, not one artifact per tenant · **Locked**

**Fork.** Hundreds of tenants run the same vendor product, branded and versioned differently. How is that represented?

**Options.**
- **Shared artifact + `TenantOverride`** (base_url + a string `label_map`).
- **Re-record per tenant.**
- **Inheritance hierarchy** — a base artifact with per-tenant subclasses overriding steps.

**Chosen.** Shared artifact + flat override.

**Why / cost.** Re-recording scales with tenants × apps — thousands of recordings, each needing its own human review, each drifting independently. A full inheritance hierarchy handles more variation but makes "what will actually execute for tenant X" a question you can only answer by resolving the chain, which is exactly the property you do not want in something a reviewer must approve. The flat override keeps the answer readable: a base URL and a handful of string remaps, about four lines. The cost is that it only handles *cosmetic* divergence. A tenant whose flow genuinely differs needs its own recording, and the override model gives no help.

**Revisit when.** Tenants diverge structurally rather than cosmetically.

## TENANT-02 — No override should degrade, not break · **Locked**

**Fork.** What happens when an artifact meets a tenant nobody has mapped?

**Options.**
- **Degrade.** Semantic locators miss, structural fallbacks catch, the run succeeds and reports drift.
- **Fail fast.** Refuse to run against an unmapped tenant.
- **Guess the mapping** at runtime.

**Chosen.** Degrade, loudly.

**Why / cost.** Fail-fast is defensible for money movement but wrong as a default here: the fallbacks genuinely work, and refusing a run that would have succeeded has its own cost. The key is that degradation is *not silent* — every fallback resolution emits a drift signal, so the run reports exactly which steps needed rescuing (scenario 08 drifts at steps 0, 2, 3, 4). That turns an invisible risk into a work item. The cost is that a run can succeed on structural fallbacks that are themselves one theme change from breaking, and nothing forces anyone to act on the drift.

**Revisit when.** Drift is being reported and ignored. The repair loop (TENANT-05) is the intended forcing function.

## TENANT-03 — Known conditions are curated per *vendor*, not per artifact or tenant · **Locked**

**Fork.** Where does the error taxonomy live, given many tenants share a product?

**Options.**
- **Per-vendor library** (`knowledge.py`), attached to every artifact for that vendor at compile time.
- **Per artifact.** Each recording carries its own.
- **Per tenant.**

**Chosen.** Per vendor.

**Why / cost.** How Corebank signals a locked account is a property of *Corebank*, not of one recorded flow or one credit union. Curating it once and inheriting it everywhere means one place to maintain and one place to review — and it is the clearest instance of cross-tenant reuse in the system. The cost is coupling: a bad condition in the library affects every artifact for that vendor at once, and the library is deep-copied on attach specifically so a caller cannot mutate the shared object.

**Revisit when.** Tenants of one vendor signal conditions differently enough that the library needs per-tenant branches — at which point the `label_map` mechanism extends to detector text.

## TENANT-04 — Drift is aggregated across runs before anything is proposed · **Settled**

**Fork.** When does a drift signal become an action?

**Options.**
- **Aggregate in a ledger**, propose only after N occurrences on the same step (default 3).
- **Act on a single drift.**
- **Report only; never propose.**

**Chosen.** Aggregate, threshold, then propose.

**Why / cost.** One drift is noise — a slow render, a one-off. The same step drifting to the same fallback run after run is a stale primary, and it is the last warning before the fallback goes too. A single `ReplayResult` structurally cannot tell those apart, so repair needs history, and history needs somewhere to live. The cost is another append-only file and a threshold that is a guess.

**Revisit when.** The threshold proves wrong in either direction in real use.

## TENANT-05 — Repair proposes; it never rewrites itself · **Locked**

**Fork.** Should the system fix its own stale locators?

**Options.**
- **Propose.** Emit a reviewable diff, bump the version, land in `draft` so the approval gate still decides.
- **Auto-apply.** Silently promote the working candidate.
- **Report only.** Name the problem, propose nothing.

**Chosen.** Propose, and inherit the existing governance rather than routing around it.

**Why / cost.** Automation that silently rewrites its own instructions for driving a bank is a worse problem than the staleness it fixes — the failure mode is a capability that quietly retargets itself and keeps reporting success. Landing the result in `draft` means the gate that already refuses unattended replay of an unapproved capability is what lets it back into production; repair borrows the governance instead of bypassing it. The cost is that a human still has to say yes — they just no longer have to *notice*.

**Revisit when.** Never, for anything touching an irreversible step.

## TENANT-06 — Repair refuses the repairs that would make things worse · **Locked**

**Fork.** Should every repeated drift be promoted to primary?

**Options.**
- **No — refuse semantic → structural promotions** and report what a human should do instead.
- **Yes,** promote whatever resolves.

**Chosen.** Refuse, with a specific diagnosis.

**Why / cost.** Drift from a semantic primary to a structural fallback has a specific meaning: the control was **renamed**, not moved. Promoting the CSS path would "fix" the symptom by permanently trading a strategy that survives rebranding for one that breaks on the next layout change — the system would be optimising itself toward brittleness, one repair at a time. So it reports "supply a tenant `label_map` for 'Member ID'" instead. The cost is that a real repair now needs a human to supply a string.

**Revisit when.** The loop can read what the renamed control *now says* and propose the `label_map` entry directly — the fourth item on the "build next" list.

---

# 6. Escalation & handoff

## ESC-01 — Control transfer happens on the *same* live session, over CDP · **Locked**

**Fork.** §3.6 requires a human to operate "the same live session the automation was using — not a fresh one". How?

**Options.**
- **CDP attach.** Launch Chromium with `--remote-debugging-port`; the operator connects over `connect_over_cdp` and drives the same page.
- **Fresh session + state replay.** Hand the operator a new browser and re-establish cookies/URL.
- **Screenshot + remote input relay** to the automation's own process.

**Chosen.** CDP attach.

**Why / cost.** This is the load-bearing detail that makes control transfer real rather than a convincing fake. A fresh session loses exactly what matters at the moment of escalation: the authenticated cookie, the half-filled form, the in-progress transaction, the current URL — and re-establishing them is itself an automation problem that can fail at the worst time. CDP preserves all of it because it *is* the same browser. The cost is a hard dependency on the browser exposing a debug port, which is why the CLI warns up front when a gated capability is launched without one (ESC-06), and why the a11y surface — which exposes no CDP endpoint — cannot host a handoff.

**Revisit when.** A surface appears with no equivalent attach mechanism. A desktop driver would need its own answer (shared session, remote input), and the control-transfer *model* below survives that change even though the mechanism does not.

## ESC-02 — An explicit control token, not an implicit convention · **Locked**

**Fork.** How does the system know who is in control?

**Options.**
- **Explicit `controller ∈ {agent, operator}`** on the intervention, flipped on raise and on resolve.
- **Implicit.** Automation blocks, so it is obviously not driving.
- **A lock file / mutex.**

**Chosen.** Explicit token, persisted with the intervention.

**Why / cost.** §3.6 asks specifically for "a way to know who is (or should be) in control", and the implicit answer only works while nothing goes wrong. If the automation crashes mid-handoff, an implicit model leaves no record of who held the session; an explicit token is on disk and answers the question afterwards. It also makes the invariant statable and auditable: while an intervention is open, the automation must not touch the page. The cost is that the token is *advisory* — nothing physically prevents the automation from acting, so this is a discipline the code follows rather than an enforcement mechanism.

**Revisit when.** Concurrent operators become possible, at which point advisory is not enough.

## ESC-03 — File-based intervention inbox · **Provisional**

**Fork.** How does an intervention request reach a human?

**Options.**
- **JSON files in a directory**, polled.
- **A queue / broker.**
- **A database with notifications.**

**Chosen.** Files.

**Why / cost.** Consistent with ARCH-03, and the intervention is already a serialisable object addressed by id — so promoting it to a real queue is a storage swap. Files also give something a queue does not: the request survives on disk after a timeout, ready for triage, with no infrastructure running. The cost is polling latency and no fan-out to multiple operators.

**Revisit when.** Operators need to be notified rather than to poll — a production requirement, not a scope one.

## ESC-04 — Blocking wait with a timeout, failing closed · **Settled**

**Fork.** What does the automation do while waiting for a human?

**Options.**
- **Block, with a configurable timeout** (default 120s), then abort and preserve the request.
- **Block indefinitely.**
- **Return immediately** and let the caller poll.

**Chosen.** Block with timeout.

**Why / cost.** The session is held open for the whole wait — that is the cost being traded, and it is why the timeout is a knob rather than a constant. Two minutes suits an unattended run where the point is to fail promptly and leave the request for triage; a person who must be fetched and briefed needs longer. Blocking indefinitely means one stuck run holds a browser and an authenticated banking session forever. Returning immediately would break the resume-on-the-same-session model, since the session must stay alive.

**Revisit when.** Operator response times are known; the default is currently a guess.

## ESC-05 — Refusals and policy blocks must never raise an intervention · **Locked**

**Fork.** Which failures are worth waking a human for?

**Options.**
- **Exclude refusals and already-escalated cases** via an explicit non-escalatable set.
- **Escalate every failure.**
- **Escalate nothing;** always fail.

**Chosen.** Explicit exclusion set, sharing its membership with the refusal set (REPLAY-02).

**Why / cost.** A person standing at a browser cannot fix a policy file, an unapproved capability, or a missing second approver — routing those to a human is a page-out with no action attached, and a system that pages for unactionable things gets muted. The exclusion set being *the same set* that defines a refusal is deliberate: one place decides what counts as "the system is fine, the request was declined", and that is the same question as "should this wake someone".

**Revisit when.** Never. This is an alerting-hygiene decision as much as a design one.

## ESC-06 — Abort immediately when no live session was exposed · **Settled**

**Fork.** A run pauses for a human, but was started without a CDP port. Now what?

**Options.**
- **Abort immediately**, saying exactly why, and preserve the request for triage.
- **Wait out the full timeout** and then abort.

**Chosen.** Abort immediately.

**Why / cost.** Taking control requires attaching over CDP. If the run never exposed one, there is no path by which *any* operator can resolve the request — so blocking is a guaranteed dead wait that delays the failure without changing it, while holding a session open. The CLI additionally warns at launch, before anything runs, when a gated capability is started without a port. The cost is nil; this is strictly better than waiting.

**Revisit when.** Never.

## ESC-07 — One worker thread owns the live session · **Locked**

**Fork.** The operator console is a web server; Playwright's sync API is thread-affine. How do they coexist?

**Options.**
- **A single worker thread owns the connection;** every request posts a command and waits.
- **Per-request Playwright connections.**
- **An async driver** to sidestep threading.

**Chosen.** Single-owner worker thread.

**Why / cost.** This is not a workaround, it is how a production console would be built: a live banking session is a single-owner resource, and pretending otherwise produces interleaved writes on a real account that are impossible to debug afterwards. Per-request connections would let two in-flight requests act simultaneously. The cost is a queue, a timeout, and a lifecycle to get right — including the case where control has been handed back and the worker no longer exists, which must answer immediately rather than block every polling request into a timeout.

**Revisit when.** Never, while the driver is thread-affine.

## Compact register — escalation & handoff

| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| ESC-08 | Human actions are persisted as they happen, not at resolve | Batch and write on resolve | A console that dies mid-handoff would lose the record of what a person already did to a live account — the one part of this flow that cannot be reconstructed afterwards |
| ESC-09 | The console forwards *coordinates* on a picture; no selectors anywhere | Operator types selectors (the original CLI stand-in) | A bank operator does not think in selectors. Coordinate-on-image is what a real co-browsing console sends, and it exercises the same input path a desktop surface would |

---

# 7. Safety

## SAFE-01 — Layered allowlist; a URL must satisfy both layers · **Locked**

**Fork.** What is the agent permitted to touch?

**Options.**
- **Two layers.** A global policy (`config/policy.yaml`) *and* the artifact's own `target.allowed_url_patterns`. A URL must pass both.
- **Global only.**
- **Per-artifact only.**

**Chosen.** Both, with an explicit denylist checked first.

**Why / cost.** The two layers answer different questions. The global policy is the institution's standing position — never touch `/logout`, never touch the test control plane — and it must not be weakenable by an artifact. The artifact's own list is the *principle of least privilege for this capability*: a member-lookup has no business navigating anywhere its recording did not go, even if the institution permits it broadly. Requiring both means a compromised or badly-recorded artifact cannot widen its own reach. The cost is two places to misconfigure, and a failure mode where a legitimate URL is blocked by the narrower layer.

**Revisit when.** Never. This is the §3.4 requirement's core.

## SAFE-02 — Fail closed, always; a violation raises rather than warns · **Locked**

**Fork.** What happens on a guardrail breach?

**Options.**
- **Raise.** Stop the run.
- **Warn and continue.**
- **Warn, continue, and flag for review.**

**Chosen.** Raise.

**Why / cost.** In a bank, "accidentally created a sub-account" is far worse than "refused to create one" — the costs are asymmetric by orders of magnitude, and the recovery paths are not comparable (one is a retry, the other is a reversal request against a member's real account). Warn-and-continue also degrades predictably in the wrong direction: warnings accumulate, get filtered, and the guardrail becomes decorative. The cost is that an over-tight policy blocks legitimate work, which is why refusals are typed distinctly and state their `requirement` (REPLAY-02) — a refusal should tell you how to proceed legitimately.

**Revisit when.** Never.

## SAFE-03 — Irreversible actions are blocked unless explicitly approved for the run · **Locked**

**Fork.** How are risky/irreversible steps handled?

**Options.**
- **Block by default;** allow only with explicit run-level approval (`--allow-risky`), or gate behind human confirmation when the step is flagged.
- **Allow with logging.**
- **Always require confirmation,** no unattended path at all.

**Chosen.** Block by default, with two distinct escape hatches: run-level approval, or per-step human confirmation (which becomes an escalation when unattended).

**Why / cost.** Same asymmetry as SAFE-02. The two hatches are deliberately different mechanisms for different situations: `--allow-risky` is a *caller* asserting a mandate for the whole run; `requires_confirmation` is a *step* demanding a person regardless. They compose — evidence 13 shows a counter-signed $1,500 deposit clearing the value gate and then being stopped by the step gate. Never allowing an unattended path would make the system useless for its actual purpose.

**Revisit when.** Never, though *what counts as* risky is a separate and more contestable decision (SAFE-04).

## SAFE-04 — Risk classification layers a lexical and a structural signal, and the structural one corroborates rather than overrides · **Locked**

**Fork.** How do we decide an action is irreversible?

**Options.**
- **Lexical only.** Caption matches create/confirm/submit/delete/transfer.
- **Structural only.** The control submits a form and that form uses POST.
- **Both, structural corroborating** — a POST submit is risky *unless* its caption is on an explicit benign list.
- **Ask the LLM** to classify at record time.

**Chosen.** Both, with structural corroborating and a short benign-caption allowlist.

**Why / cost.** Lexical alone only knows the words it was taught — it misses "Apply", "Proceed", "Finalise", which carry no risky keyword and still change state. Structural alone is evidence about what the action *does* rather than what it is called, so it survives relabelling, translation and per-tenant branding — but promoting *every* POST would flag "Sign On" and "Search", and a guardrail that cries wolf gets switched off. Corroboration keeps the precision of the caption while catching the unlabelled mutation. An LLM classifier was rejected because the classification must be reviewable and stable across runs, not a fresh judgement each time. The cost is a curated regex per vendor, and the benign list is a maintained artifact that can go stale.

**Revisit when.** A vendor's captions defeat the benign list in either direction. Note the structural signal has a direct desktop analogue (an Invoke pattern vs a navigation), so it ports.

## SAFE-05 — The heuristic proposes; a human ratifies before unattended use · **Locked**

**Fork.** Can a heuristic's risk classification be trusted enough to run unattended?

**Options.**
- **No.** `catalog approve` refuses while any risky step is unreviewed; a reviewer may also *downgrade* with a recorded justification.
- **Yes** — trust the classifier.
- **Require review of every step,** risky or not.

**Chosen.** Human ratification of risky steps only, with the power to reclassify.

**Why / cost.** A heuristic may promote a capability to `draft`; it may not promote it to `approved`, because approval is precisely the point at which unattended replay of an irreversible action becomes possible. Letting the reviewer *change* the class — not just tick a box — is what makes it a review rather than a rubber stamp; an over-eager structural signal being downgraded with a reason is the system working. Requiring review of every step would make the gate expensive enough to be routed around. The cost is a human in the deployment path for every capability with a risky step.

**Revisit when.** Never.

## SAFE-06 — A value-level policy layer, separate from the allowlist · **Locked**

**Fork.** The allowlist can say "may the agent click here". What says "is this amount sane"?

**Options.**
- **A third layer** inspecting the *inputs* a capability is invoked with, before the browser opens.
- **Encode limits in the artifact** per capability.
- **Leave it to the calling agent.**

**Chosen.** A separate input-inspecting layer, keyed by parameter name in the global policy.

**Why / cost.** URL/action allowlists are structurally incapable of distinguishing a $1 transfer from a $1M one — they operate on a different axis entirely, and this was the gap the first version explicitly did not have. Keying by *parameter name* in the global policy means a capability declaring `deposit` inherits the institution's limits without knowing they exist, which is the right direction of authority: limits belong to the institution, not to the capability that happens to move the money. Leaving it to the calling agent puts the control on the wrong side of the trust boundary. The cost is that a parameter must be named consistently across capabilities for its limit to apply.

**Revisit when.** Parameter naming diverges across vendors — at which point the rules need a mapping layer.

## SAFE-07 — A hard ceiling is refused outright and never escalated · **Locked**

**Fork.** What happens above `max`?

**Options.**
- **Refuse.** No escalation path, no override.
- **Escalate** to a senior approver.
- **Refuse, but allow a documented break-glass.**

**Chosen.** Refuse outright.

**Why / cost.** No operator standing at a browser should be able to wave through an amount the institution has already ruled out — if the ceiling is overridable in the moment, it is not a ceiling, it is a speed bump, and the moment of pressure is exactly when it will be overridden. Changing the limit is a policy change with its own review, which is the correct path. The cost is real inflexibility: a legitimate large transaction cannot proceed through this system at all, and must be done another way.

**Revisit when.** Never, without a break-glass that is itself dual-controlled and audited — and that is a policy decision for the institution, not an engineering one.

## SAFE-08 — Dual control requires two *different*, *authorised* people · **Locked**

**Fork.** What makes a second approver real?

**Options.**
- **Two conditions:** independence (approver ≠ initiator) *and* registry membership.
- **Independence only.**
- **Registry only.**

**Chosen.** Both, and they are not the same check.

**Why / cost.** Independence stops a run approving itself. Registry membership stops any typed string counting as a second pair of eyes — without it `--approver whoever` is theatre. Each alone leaves an obvious hole. The registry stands in for the institution's directory; binding it to authenticated identity changes *where the set comes from*, not the shape of the check. The cost, and it is the significant one: both names are currently self-asserted strings on a command line with no authentication behind them (OPEN-03). The *check* is sound; the *identity* is not yet.

**Revisit when.** Immediately, for the identity half. See OPEN-03.

## SAFE-09 — Unattended dual control fails closed · **Locked**

**Fork.** Over the dual-control threshold with nobody counter-signing. Proceed or refuse?

**Options.**
- **Refuse.**
- **Proceed and flag** for after-the-fact review.
- **Escalate and wait.**

**Chosen.** Refuse — after trying to escalate if an operator is reachable.

**Why / cost.** Unattended is precisely the situation in which a second pair of eyes cannot be assumed, so proceeding would make the control vacuous exactly where it is most needed. The resolution order is preference-ordered: a named independent approver proceeds and is recorded; otherwise an operator is given the chance to counter-sign; otherwise refuse. After-the-fact review of an irreversible action is not a control, it is a report.

**Revisit when.** Never.

## SAFE-10 — Velocity is bounded across runs, scoped by parameter · **Locked**

**Fork.** A per-invocation ceiling cannot see ten transactions in a row. What does?

**Options.**
- **A rolling-window sum** against a persistent ledger, scoped by *parameter* across capabilities.
- **Per-invocation ceilings only.**
- **A rolling window scoped per capability.**

**Chosen.** Rolling window, scoped by parameter.

**Why / cost.** Ten $999 deposits inside a minute clear a $1,000 limit ten times over, and that is the shape most real money-movement abuse takes — so a limit with no memory is not a limit. Scoping per *capability* rather than per parameter is the tempting simplification and it is precisely the gap an attacker walks through: two different flows that both move money would each get a full budget. The cost is that the budget is shared, so an unrelated capability can exhaust it — which is the correct behaviour but will surprise callers.

**Revisit when.** Never in shape. The *storage* is provisional (SAFE-11).

## SAFE-11 — Amounts are booked only on success · **Locked**

**Fork.** When does a value-bearing run charge the velocity budget?

**Options.**
- **On success only.**
- **On attempt.**
- **On attempt, refunded on failure.**

**Chosen.** On success.

**Why / cost.** A refused, escalated or failed run moved no money, so charging it would starve the budget with runs that never happened — a guardrail that denial-of-services the thing it protects. Booking on attempt with a refund is more correct under crash conditions but needs transactional semantics the file-backed ledger does not have. The cost is the crash window: a run that succeeds in the UI and then dies before booking is unrecorded, so the budget slightly under-counts. Under-counting favours availability over strictness, which is the wrong direction for a safety control and is an accepted, documented limitation of ARCH-03.

**Revisit when.** The ledger becomes transactional (OPEN-05).

## SAFE-12 — A governed value that will not parse is refused · **Settled**

**Fork.** `deposit="five hundred"` against a numeric ceiling.

**Options.**
- **Refuse.**
- **Pass it through** — the UI will reject it.
- **Best-effort coercion.**

**Chosen.** Refuse.

**Why / cost.** A limit you cannot evaluate is not a limit. Passing an unparseable value through means the guardrail silently does not apply to it, which converts a malformed input into a bypass. The cost is that a legitimately odd format ("USD 500") is refused; the parser tolerates currency symbols and separators to keep that narrow.

**Revisit when.** Real inputs show formats the parser should accept.

## SAFE-13 — Redaction is two-layered, and the observation never captures typed values · **Locked**

**Fork.** How do secrets and regulated data stay out of artifacts and logs?

**Options.**
- **Prevent at the source** (never capture a control's value into an observation) **plus** name-based redaction **plus** pattern-based patterns as a net.
- **Redact at the log boundary only.**
- **Pattern-based only.**

**Chosen.** All three layers, with prevention first.

**Why / cost.** The most important of the three is the one that is easy to overlook: the element indexer deliberately never reads `el.value`, so a typed credential never enters an observation, never reaches the model prompt, and never reaches a log — there is nothing downstream to redact. Name-based redaction handles declared secrets; the pattern net (SSN, card, email) catches what slips into free text from page snapshots. The compiler adds a fourth pass, scrubbing run-specific input values and extracted outputs out of step *intents*, because the model narrates in prose and would otherwise bake one run's data — potentially a real credential it echoed — into a committed artifact. The cost is over-redaction, which is the deliberate bias, softened by naming the parameter in the placeholder so it reads as intentional rather than as corrupted evidence.

**Revisit when.** Never in shape; the sweep is enforced by `tests/test_artifact_hygiene.py` so it cannot silently regress.

## SAFE-14 — Card detection is Luhn-checked · **Settled**

**Fork.** A regex for 13–19 digit runs will fire on timestamps and run ids.

**Options.**
- **Luhn-check the candidate** before redacting.
- **Redact every long digit run.**
- **Drop card detection.**

**Chosen.** Luhn check, plus a lookbehind so hyphenated run ids are not candidates at all.

**Why / cost.** An arbitrary 14-digit string satisfies Luhn about 10% of the time, which turns a noisy pattern into a usable one. This decision has a concrete history: the pre-Luhn version mangled run-id timestamps into `***CARD***` in committed evidence, destroying the run identity in `run.jsonl` — over-redaction is the right bias but it is not free, and evidence that has been corrupted cannot be un-corrupted. The cost is that a card failing Luhn (a typo'd one) is not redacted.

**Revisit when.** Never.

---

# 8. Observability

## OBS-01 — Every run gets its own directory: structured log, summary, screenshots, DOM on failure · **Settled**

**Fork.** What evidence does a run leave behind?

**Options.**
- **Per-run directory** with `run.jsonl` (ordered structured events), `summary.json` (the result contract), screenshots, and DOM snapshots on failure.
- **A single append-only log** for all runs.
- **Structured events only,** no visual evidence.

**Chosen.** Per-run directory.

**Correction (round 2).** This entry originally said "per-step screenshots", which is true of DISCOVERY and false of REPLAY: replay captures on notable events only -- a business outcome, a hard failure, a gated step, an assisted recovery, a completed intervention. A clean happy-path replay therefore leaves no image at all, which is defensible (screenshots are the one artifact that can carry PII off a member's screen, see REPORT §6) but was not what this entry claimed. A demo-only `--capture-steps` flag is the obvious next move if watching a successful run frame by frame matters more than the exposure.

**Why / cost.** §3.5 asks for a structured log *plus* at least one richer signal on failure. Per-run isolation means an investigation starts by opening one directory rather than filtering a shared log, and the run is self-contained enough to hand to someone else. The cost is disk and directory sprawl — which materialised: ad-hoc CLI runs accumulated in the reviewer-facing `evidence/` until they buried the curated scenarios (FIX-05).

**Revisit when.** Retention becomes a real concern, which it will in production long before it does here.

## OBS-02 — Events are flushed per event, not buffered · **Settled**

**Fork.** When does a log event hit disk?

**Options.**
- **Flush every event;** keep the handle open for the run's lifetime.
- **Buffer and write on completion.**
- **Context manager per event.**

**Chosen.** Flush per event.

**Why / cost.** A run's log has to survive the run crashing — and a crash is exactly when the log matters most. Buffering loses the final events, which are the interesting ones. The cost is a long-lived file handle that lint flags (`SIM115`, suppressed with a documented reason) and an fsync per event, which is irrelevant at this volume.

**Revisit when.** Event volume makes per-event flushing a bottleneck.

## OBS-03 — All evidence text passes through redaction; screenshots are treated as sensitive · **Locked**

**Fork.** Evidence is the thing most likely to leak regulated data. What is the policy?

**Options.**
- **Redact all text** (events, summaries, DOM snapshots) and treat screenshots as sensitive evidence with no masking.
- **Redact text and mask screenshots.**
- **Do not capture screenshots.**

**Chosen.** Redact all text; screenshots captured unmasked and declared as sensitive.

**Why / cost.** Screenshots are the single most useful failure signal and the single hardest to redact — masking requires knowing where PII is rendered, which on a legacy table-soup page is its own detection problem. Not capturing them loses the evidence that makes a failure diagnosable. So the decision is to capture, and to be explicit that the evidence store inherits the sensitivity rather than pretending the data is clean. This is stated as a limitation in REPORT §6 rather than hidden. The cost is that `evidence/` contains member names and balances as pixels, and today has no access control (OPEN-06).

**Revisit when.** Round 2 — masked capture plus a restricted evidence store is the production answer and is a named next step.

## OBS-04 — Provenance binds an artifact to its transcript by path *and* hash · **Locked**

**Fork.** How does a reader confirm an artifact came from the run it claims?

**Options.**
- **`transcript_ref` + `transcript_sha256`,** transcript stored separately.
- **Inline the transcript** in the artifact.
- **Path reference only.**

**Chosen.** Path plus hash.

**Why / cost.** Inlining bloats the contract with evidence and couples the capability to model verbosity (ARCH-07). A path alone can silently point at a file that has changed, or at nothing at all. The hash is what makes provenance a *checkable claim* rather than an assertion. The cost is that the binding rots if either side moves — which it did: the evidence directories were renamed and the transcripts re-redacted, and nothing noticed until an audit (FIX-08). A hash nobody verifies is decoration, which is why there is now a test.

**Revisit when.** Never.

## OBS-05 — Cross-run state lives outside any single run's directory, and is not committed · **Settled**

**Fork.** Where do the drift ledger and value ledger live?

**Options.**
- **Beside the run directories,** gitignored as regenerable run state.
- **Inside each run's directory.**
- **Committed** as part of the evidence.

**Chosen.** Beside, gitignored.

**Why / cost.** A velocity budget scoped to a single run is not a velocity budget — the whole point is memory across runs, so it cannot live inside one. Committing it is actively harmful: a checked-in ledger carrying a seeded spend would silently consume a reader's budget before they ran anything, and would make a documented demo refuse for invisible reasons. The cost is that the evidence for a velocity refusal must be self-explaining without the ledger, which is why the refusal `reason` carries the arithmetic (`4500 already spent`, `would take the total to 5400`, `over 5000`).

**Revisit when.** The ledger becomes a system of record rather than a demo artifact.

---

# 9. Agent-facing surface

## AGENT-01 — Capabilities are exposed as a function-calling manifest · **Settled**

**Fork.** How does a calling AI agent discover and invoke a capability?

**Options.**
- **A manifest** derived from the artifact's typed contract — name, description, input schema, returns.
- **Hand the agent the artifact** and let it read the steps.
- **A bespoke RPC interface** per capability.

**Chosen.** Derived manifest.

**Why / cost.** The through-line of the whole system is that the agent invokes a capability *without re-reasoning about the UI* — so the agent must never need to read the steps. The manifest is generated from `inputs`/`outputs`/`description`, which is exactly the contract SCHEMA-01 made primary; it falls out for free precisely because the schema was shaped that way. The cost is that the manifest is only as good as the descriptions the discovery task supplied.

**Revisit when.** Agents need richer negotiation than name-and-typed-args.

## AGENT-02 — Refusal maps to HTTP 403, failure to 422 · **Settled**

**Fork.** How does the result contract surface over HTTP?

**Options.**
- **Distinct codes per status:** success/business_outcome → 200, refused → 403, escalated → 409, failure → 422.
- **200 for everything,** status in the body.
- **500 for anything non-success.**

**Chosen.** Distinct codes.

**Why / cost.** This carries REPLAY-02's distinction across the wire, where it matters most: a refusal is not a server fault, and a caller's retry logic must not treat it as one. 500-for-everything would make every policy decision look like an outage to any monitoring in front of this. The cost is that a business outcome returns 200, which some callers will find surprising — but it is correct: the call succeeded, the answer was "no such member".

**Revisit when.** Never.

## Compact register — agent-facing surface

| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| AGENT-03 | Unapproved capabilities are refused by default over the API | Allow with a warning header | The API is the unattended path; the approval gate matters most exactly there. An explicit `allow_unapproved` exists for testing |
| AGENT-04 | Codegen is an *export*, not the execution path | Generate code as the primary artifact | The JSON artifact plus the replay engine try fallbacks and classify runtime conditions; a generated script uses only the primary locator and knows nothing about the error taxonomy. Useful for review and for teams that prefer committed code — but strictly less robust, and labelled as such |
| AGENT-05 | Stability is a signal on the artifact, not a gate by itself | Auto-approve above a pass-rate threshold | A pass rate measures flakiness, not safety. It informs a human's approval decision; it does not replace it |

---

# 10. Corrections (post-review)

Decisions made while fixing a hostile read of the round-1 build. Each entry is a
claim the code did not actually enforce.

## FIX-01 — Readout locators lead with the portable candidate · **Locked**

**Fork.** Extraction targets were recorded with an XPath primary, so no surface without a DOM could honour them — while `portability_report` reported the artifact portable, because the *interactive* steps were fine.

**Options.** Prepend a `NEAR_LABEL` candidate; leave it and narrow the portability claim; drop the second-surface claim.

**Chosen.** Prepend, and teach the web surface to resolve `NEAR_LABEL` on a read-only cell (it previously matched only form controls, so the candidate would have been decorative — recorded, never resolved).

**Why / cost.** The committed artifacts had `near_label` on extracts but the pipeline produced XPath-only ones, so a regenerated artifact was *not* portable and the flagship §4 demo depended on artifacts the build could not reproduce. Cost: none — the DOM-bound form is still recorded second.

**Revisit when.** Never. `test_readout_locators_lead_with_the_portable_candidate` pins it.

## FIX-02 — Codegen covers the whole locator vocabulary · **Settled**

**Fork.** `_loc_expr` had no case for `NEAR_LABEL`, so it fell through to `ctx.locator(value)` — a valid CSS query for a nonexistent element. The script imported, compiled, ran, and matched nothing.

**Options.** Add the missing case; drop codegen; keep it and document the gap.

**Chosen.** Add the case, and a completeness test — which immediately found three more kinds with the same defect (`alt_text`, `title`, `test_id`).

**Why / cost.** The old test asserted the output *parsed*, which is exactly how this survived. Asserting "it parses" cannot see a wrong selector; running it can.

**Revisit when.** Never.

## FIX-03 — The velocity ledger is wired into both invocation paths · **Locked**

**Fork.** `Ledger` was never constructed anywhere — not the CLI, not the service, not the tests. `max_per_window` in the policy file was dead config, while REPORT §6 described it as a working control.

**Options.** Wire it; delete the feature; document it as unimplemented.

**Chosen.** Wire it into the CLI and the service.

**Why / cost.** A guardrail nothing constructs is configuration, not a control — and the write-up claimed otherwise, which is worse than not having it.

**Revisit when.** When the ledger needs to be transactional (OPEN-05).

## FIX-04 — Stability write-back refuses a tenant-bound run · **Settled**

**Fork.** `--tenant … --update-stability` wrote the *rebased* artifact back over the shared one, silently converting a multi-tenant capability into a single-tenant one.

**Options.** Write back the original object; refuse; store per-tenant scores.

**Chosen.** Refuse, and say why.

**Why / cost.** Two reasons, either sufficient: the object is rebased, and a pass rate measured on one tenant is not evidence about the capability. Per-tenant scores are the real answer and need a schema change nobody has asked for.

**Revisit when.** Stability becomes per-(capability, tenant) in the schema.

## Compact register — corrections

| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| FIX-05 | Artifacts ship `draft`, with approval as a documented step | Ship approved so the demo runs immediately | Approval is a human act against a deployment; shipping pre-approved disarms the gate for everyone who clones the repo. Enforced by `test_committed_artifacts_ship_in_their_as_discovered_review_state` |
| FIX-06 | Ad-hoc run directories gitignored by capability-id pattern | Manual cleanup | 19 timestamped directories had buried the curated evidence. Curated scenarios are numbered and never contain a dot; capability ids always do |
| FIX-07 | `replay/errors.py` renamed to `transforms.py` | Leave it | It contained value transforms and no errors, in a system whose error model is the point |
| FIX-08 | Provenance repointed and rehashed, with the reason recorded | Leave the dangling ref; re-run discovery live | Both `transcript_ref` and `transcript_sha256` had drifted, so the integrity check silently verified nothing. Now enforced by `test_provenance_binds_each_artifact_to_a_transcript_that_exists_and_verifies` |

---

# 11. Adaptation to MERIDIAN CORE

## MER-01 — Grid reading rides `Extraction.attribute` · **Locked**

**Fork.** A member's balances live in a grid whose row count varies. No fixed set of scalar extract steps can express that.

**Options.**
- **`attribute="table"`** plus one `ValueType` — reading a grid is a property of the element, which is what `attribute` already selects.
- **A new `Extraction.shape` field** plus a parallel extraction path.
- **Return the grid as text** and let callers parse.
- **Parameterise by share** — `member_balance(member_id, share_id)`.

**Chosen.** `attribute="table"`, `ValueType.TABLE`, and a second typed channel `ActResult.rows`.

**Why / cost.** Text abandons the typed contract that is the artifact's whole point. Parameterising by share cannot answer "what are this member's balances?" and cannot tell a caller which shares exist — so the transfer capability would have no way to discover its own arguments. The `shape` field was the first design and was rejected as larger than necessary: perception belongs in the surface, so `attribute` was already the right field, and this cost two enum extensions instead of a new extraction path. `rows` is separate from `value` so every existing caller keeps its `str` contract.

**Revisit when.** A capability needs a single cell rather than the grid. Today the caller filters, which returns more regulated data than strictly asked for — a real if minor cost.

## MER-02 — Conditions can be scoped by the action that preceded them · **Locked**

**Fork.** MERIDIAN renders `TRANSACTION REJECTED` for a genuine validation failure *and* when its fault injector fires on a plain page load.

**Options.**
- **Scope by action** (`applies_to_actions`), so the same text carries two classifications.
- **Business outcome always** — a random blip during a navigation is reported as "the bank rejected your transaction", about a transaction never submitted.
- **Recoverable always** — a real rejection is retried pointlessly, then surfaced as a recovery failure, which reads as a system fault.
- **A more specific detector** — there isn't one; the pages are identical.

**Chosen.** Scope by action, with the scoped business outcome declared first and an unscoped recoverable entry after it.

**Why / cost.** Text alone must be wrong half the time. The scope check runs *before* detection so an inapplicable condition cannot consume the match and shadow the one that applies. Cost: declaration order now carries more meaning, which `test_specific_conditions_precede_the_generic_page_that_would_swallow_them` pins.

**Revisit when.** Never.

## MER-03 — Recovery is refused on irreversible steps · **Locked**

**Fork.** MERIDIAN injects transient faults on posting actions at a configurable rate. Our `APPLICATION_ERROR` recovery is a reload.

**Options.**
- **Never retry a risky step** — hard failure, human reconciles.
- **Retry everything** — the pre-existing behaviour.
- **Confirm-then-retry** — check whether the post landed, then decide.

**Chosen.** Never retry a risky step, behind a named opt-in (`allow_recovery_on_risky_steps`, default false).

**Why / cost.** Reloading a posted transfer is how one transfer becomes two, and the error page cannot tell us whether the post landed before the fault. Confirm-then-retry is the correct long-term answer and needs a per-capability "how do I tell if this landed" probe — new schema surface that deserves its own design pass rather than being added mid-sprint. Cost: a legitimately transient fault on a post now fails hard.

**Revisit when.** The confirmation probe exists.

## MER-04 — Stuck detection keys on repetition, not stillness · **Locked**

**Fork.** The round-1 rule escalated whenever four steps shared a URL. Every MERIDIAN flow with a form wider than three fields escalated instead of recording.

**Options.**
- **Signature of `(url, action, target)`** repeated N times.
- **Only count navigating actions** — fixes the forms, but lets a model fill one field forever.
- **Raise the threshold** — delays both problems without fixing either.

**Chosen.** Signature repetition.

**Why / cost.** One rule catches both a control that does nothing and a model spinning on one field, while treating five different fields as the progress they are. The middle option was implemented first and rejected when the round-1 test for a fill-spin started failing — the test was right and the fix was too narrow.

**Revisit when.** Never.

## MER-05 — The agent is blocked from the target's fault-injection screen · **Locked**

**Fork.** The target's error behaviour is governed by a settings page reachable from the same session the automation drives.

**Options.** Block it in the artifact and global allowlist; allow it; ignore the question.

**Chosen.** `blocked_url_patterns` includes `*/settings*`; a separate harness tool (`scripts/meridian_control.py`) sets fault state, speaking HTTP directly so it cannot be mistaken for a capability.

**Why / cost.** An automation that can switch off its own error conditions can hide its own failures, and a green run would stop meaning anything. The harness may set up the world; the automation may not change the conditions it is judged under. Cost: scenarios must arrange their own fault state explicitly.

**Revisit when.** Never — this generalises to any target with a test-control plane.

## MER-06 — All authorisation moved server-side · **Locked**

**Fork.** The API accepted `allow_risky`, `allow_unapproved` and a password in the request body.

**Options.**
- **Server-side config** (`config/service.yaml`) for risk, approval, role and operator allow-lists; alias-based credential resolution.
- **Keep request-supplied flags** and trust callers.
- **Sign requests** so only authorised callers may set the flags — solves who asks, not what they may ask for.

**Chosen.** Server-side, with nothing the caller sends consulted in the decision.

**Why / cost.** With a chatbot in front, request-supplied flags mean a language model authorising a funds transfer, and a caller choosing which operator to be — the application's own boundary intact while our wrapper walks around it. §3.5 names this exact failure. Cost: adding a capability now requires a config entry, and an absent entry means "deny" — deliberately, since an unconfigured deployment should be able to do nothing rather than everything.

**Revisit when.** Never in shape; the credential *store* is provisional (OPEN-02).

## MER-07 — A reviewer may separate irreversibility from per-run confirmation · **Locked**

**Fork.** `requires_confirmation` always beat `allow_risky`, so no irreversible step could ever complete unattended — making `transfer_funds` unusable, against §2.1's minimum bar.

**Options.**
- **A reviewer clears `requires_confirmation` per step**, with a recorded reason, during `catalog review`.
- **Let `allow_risky` satisfy confirmation** — weakens the gate globally with no per-step judgement.
- **A policy flag** — same weakening, one level further from the step it affects.
- **Always escalate** — correct-looking, but `place_hold` and `transfer_funds` are not the same risk and treating them identically means the demo stalls on both.

**Chosen.** Per-step, human-only, recorded on the artifact.

**Why / cost.** They are different questions. A transfer is irreversible *and* bounded by an amount ceiling, a velocity budget and dual control — an envelope, not a person, is what stops a bad one. A hold has no such envelope, so it keeps its per-run confirmation and always escalates. Cost: one more thing a reviewer must decide, which is the point.

**Revisit when.** Never.

## MER-08 — Capabilities are stateless, including sign-on · **Locked**

**Fork.** Every capability signs on fresh; a "look up then transfer" conversation signs on twice.

**Options.** Stateless; session reuse via a pool; a session-handle capability.

**Chosen.** Stateless.

**Why / cost.** A capability must be independently invocable and independently auditable, and a shared session means one capability's failure poisons another's. It also makes "Member Inquiry / Selection" impossible as a standalone capability — it would leave UI state no later invocation can see — which is why inquiry folds into `member_lookup`. Cost is real and stated rather than hidden: extra latency, and more exposure to the target's fault rate per capability.

**Revisit when.** The cost is measured rather than assumed. Session reuse is a named next step behind a seam.

## MER-09 — `member_search` selects nothing · **Locked**

**Fork.** Both search modes land on a results list, so a recorded "Select" click on a multi-row result resolves to an arbitrary row.

**Options.** Return candidates and let the caller choose; select the first match; select by member number with a parameterised row locator.

**Chosen.** Return candidates; `member_lookup` handles the by-number case where the list has one row.

**Why / cost.** Acting on the wrong member's account is the worst error this system could make, and "first row" is a silent way to do it. It also gives the chatbot the right behaviour for free — "I found two members, which one?" Cost: two calls for a name-based lookup.

**Revisit when.** Never.

## Compact register — adaptation

| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| MER-10 | Vendor taxonomies loaded from YAML | Python literals (round 1's shape) | The people who know a vendor's screens are not the people who know this engine; adding MERIDIAN became a config file rather than a patch |
| MER-11 | `catalog refresh-conditions` bumps the version and lands in `draft` | Load the taxonomy at replay time | An approved capability's behaviour must not change because someone edited a shared file — approval was granted against specific behaviour |
| MER-12 | Label/value pairing is left-to-right adjacency | First cell paired with last cell | MERIDIAN packs two pairs per row, so first-to-last returned the phone number for `E-mail:` — plausible, wrong, and silent |
| MER-13 | `SUPERVISOR_OVERRIDE_REQUIRED` detects the denial phrase, not the page heading | The heading, which is what the brief's wording suggests | The heading is also a standing warning on the hold form at HTTP 200 for *every* operator, so detecting it told supervisors they were not authorised |
| MER-14 | `update_member_info` returns the record read back, not the banner | Return the acknowledgement text | The banner says a write was accepted; the record says it landed — the same reason a fill is verified by reading the control |
| MER-15 | Discovery traces address controls by name, not integer ref | Hand-authored ref indices (round 1's shape) | A trace of integers is unreadable and retargets silently when a page gains a control; a named target fails loudly instead of clicking the wrong button |
| MER-16 | Chatbot default router is deterministic | LLM-only routing | The demo must not require a key, and a readable mapping cannot surprise anyone. `LLMRouter` exists and returns the same object |
| MER-17 | Chatbot reaches the core only over HTTP | Import the catalog directly for speed | It is what makes "cannot bypass server-side checks" structural rather than aspirational; asserted by parsing the module |
| MER-18 | Dashboard is a read-only projection | Give it a database | A second account of what happened would eventually disagree with the engine's, and the trusted one would be whichever is easier to edit |
| MER-19 | Flask JSON key sorting disabled | Accept the default | A grid's column order is part of its meaning; the default silently alphabetised every extracted table |
| MER-20 | Identifier stripping is share-then-member | Either order | Stripping members first mangles `100234-S0070` into `-S0070`, leaving `0070` to be read as an amount — a request naming no amount would have transferred $70 |
| MER-21 | Evidence 05 uses an amount under the dual-control threshold | A large overdraw | At $4,000 our own gate refuses first and the run never reaches the host, demonstrating the wrong layer |

---

# 12. Open decisions (awaiting requirements)

Analysis prepared, deliberately not built. Each names what a deployment would
have to decide.

| ID | Question | Options | Leaning |
|---|---|---|---|
| OPEN-01 | Authentication on the API, chatbot and dashboard | mTLS between services; an API gateway with OIDC; signed service tokens | A gateway — it keeps identity out of this codebase, which has no business owning it |
| OPEN-02 | Where operator secrets live | Vault/Secrets Manager; the institution's directory; OS keychain | The `CredentialStore` seam already isolates this; the choice is the institution's, not ours |
| OPEN-03 | Binding `initiator`/`approver` to real identity | Propagate the gateway's authenticated subject; sign the approval separately | Propagate — a dual control built on self-asserted strings is theatre, and this is the largest remaining gap |
| OPEN-04 | Evidence store access control | Restricted bucket + retention; masked capture at source; both | Both. Screenshots of member accounts are the most sensitive thing this system produces |
| OPEN-05 | Ledger integrity and concurrency | SQLite with transactions; the institution's ledger of record; append-only log with fsync + locking | The institution's ledger — ours should not be the system of record for money movement |
| OPEN-06 | Concurrent gated runs | CDP port pool; one escalation at a time; queue interventions | A port pool is straightforward; the harder question is which operator owns which session |
| OPEN-07 | Confirm-then-retry on irreversible steps | Per-capability "did it land" probe; idempotency key echoed by the host; never retry (today) | The probe, as a schema addition with its own design pass |

---

# 13. Live-model findings

Decisions forced by running a real model against the real target. Each one is a
defect that only a live run could surface: a scripted trace does exactly what it
is told, so it never asks for a password, never omits a ref, and never selects an
option by the wrong handle.

## LIVE-01 — A failed action never reaches the transcript · **Locked**

**Fork.** A `select` timed out during a scripted recording. The step was written into the transcript with `ok=False`, compiled into the artifact, and the run reported **SUCCESS** because the final checkpoint happened to pass anyway.

**Options.**
- **Correct and retry, and refuse to compile a transcript containing a failure.**
- **Abort discovery on any failed action** — a transient fault then kills a long run.
- **Compile only the successful steps** — the artifact is then missing a step the flow needs, and nothing says so.
- **Leave it** — the pre-existing behaviour.

**Chosen.** Bounded correct-and-retry in the loop, plus a hard refusal in the compiler.

**Why / cost.** This breaks the invariant the entire design rests on: ARCH-05 says discovery acts *through* the locators it records so that what we record already worked. An artifact containing a step that has never succeeded is indistinguishable from a good one until it is replayed against a member's account. Enforced in two places deliberately — the loop prevents it, the compiler catches a regression in the loop.

**Revisit when.** Never.

## LIVE-02 — Text controls report emptiness, not content · **Locked**

**Fork.** A live model filled the same search box four times and was stopped by the stuck detector. The observation deliberately never exposes a control's typed value (SAFE-13), so a filled box and an empty one look identical.

**Options.**
- **Report `empty` / `has a value`** and nothing else.
- **Expose the value** — abandons the guarantee that a credential never reaches the model or the logs.
- **Expose a length or a masked prefix** — leaks a little, which is the wrong shape of answer for a password.
- **Prompt-only** ("rely on history") — advice a model may ignore, against a blind spot it cannot see around.

**Chosen.** Emptiness only, plus the prompt line.

**Why / cost.** Enough to know a step is done; carries no part of the secret, not even its length. The security decision was right and the blind spot it created was an oversight, not a trade — this closes the second without weakening the first.

**Revisit when.** Never.

## LIVE-03 — The manifest omits credentials and service-supplied identity · **Locked**

**Fork.** Given the full manifest, a live model **stopped and asked the user for the operator's password** instead of calling any tool — because `password` was listed as a required input.

**Options.**
- **Filter `sensitive` inputs and anything the service supplies** out of the published manifest.
- **Leave them and force a tool call** — hides the symptom; the contract still says a caller should send a password.
- **Mark them optional** — still advertises that the capability accepts one.

**Chosen.** Filter, always, at the catalog with the service naming what it supplies.

**Why / cost.** A public tool list saying "this capability takes a password" is an invitation to send one, and the whole credential design exists so that no caller ever holds one. This was the manifest working exactly as written and exactly wrong. Cost: `catalog manifest` on the bare CLI shows a slightly different set from the service's, because only the service knows what it fills in.

**Revisit when.** Never.

## Compact register — live-model findings

| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| LIVE-04 | `select_option` tries the recorded handle, then the other, then a leading-code prefix — and reports which worked | Fail on the recorded handle | Legacy options render `"CODE - Description ($balance)"` over a bare value; which one a recording names is a property of the markup, not the intent. Reporting the fallback keeps a recording that only works via it from looking correct |
| LIVE-05 | The compiler binds a decorated option label to its parameter by prefix | Exact match only | A live recording carried `'100234-S0070 - Share Draft (Checking) ($232.55)'` as a literal — a **balance** welded into the artifact, which could never replay |
| LIVE-06 | A malformed model action is corrected and retried, bounded | Escalate immediately | A ten-step run that had already extracted three of four outputs was thrown away because one tool call omitted a ref |
| LIVE-07 | The LLM router uses `tool_choice: auto`, not `any` | Force a tool call | Forcing meant "what is the weather in Denver" routed to a sign-on attempt against a bank. Declining is a valid answer |
| LIVE-08 | A share id that does not name its member is asked for, never repaired | Prefix it with the member id | A model returned `from_share: "S0070"`; repairing it would let the surface's prefix matching pick whichever option started with it, on a live transfer |
| LIVE-09 | The dashboard invokes through the public API | Call the replay engine directly | Faster, identical on screen, and bypasses every server-side check — a reviewer would be watching a privileged path no real caller has |
| LIVE-11 | URL and text checkpoints match on a BOUNDARY, never bare containment | Substring `in` | `/members/1002` "matched" `/members/100234`, so a lookup for a member who does not exist returned a different member's balances as `status: success`. Found by searching the target by hand, not by any test |
| LIVE-12 | An ambiguous or inexact member number is a business outcome, not a failure | Hard-fail the checkpoint (safe but wrongly typed) | Several records matching is a QUESTION needing more information, and reporting it as a fault sends someone to investigate a search that worked. Three uncertain cases, three typed answers |
| LIVE-13 | Detectors are parameterised, and conditions can be scoped by URL | Static detector text only | A condition comparing against the caller's OWN input cannot be written otherwise, and "the number is absent" is trivially true on the main menu |
| LIVE-14 | Conditions may require several detectors at once (`also_requires`) | One detector per condition | "The number is absent" also holds on the transfer form, which does not display it — the single-clause version fired on the very screen the caller asked for and broke four unrelated scenarios |
| LIVE-15 | The chat router says WHY a number was rejected, not that it is missing | Report it as absent | "I still need a member number" reads as though none was given; the user retypes the same thing. It is also the cheapest of four places this danger is caught |
| LIVE-10 | Run history indexes discovery runs alongside replays, labelled by kind | Replays only | How a capability was *found* is evidence a reviewer wants, and the two shapes are normalised once rather than the dashboard learning both |

## AUTH-01 — The session token carries an identity, never a permission · **Locked**

**Fork.** A signed-in console needs a session. What does the token say?

**Options.**
- **Username + expiry only**, with role, member scope and delegated alias re-resolved from the principal store on every request.
- **Claims in the token** (role, member id, alias) — one signature check and no lookup, the usual JWT shape.
- **Server-side session table** — a store to look up, and a second thing that can disagree with the principal file.

**Chosen.** Username and expiry, re-resolved every time.

**Why / cost.** A token that carries a role is a token whose signing key is worth a role. Carrying only a name means forging one still requires naming somebody a store recognises, and it means a demotion or a revocation takes effect on the **next request** rather than at the next sign-in — which is what an operations team actually needs when something is going wrong. Cost: a file read per request. `PrincipalStore` already reads fresh for exactly this reason, and the file is small.

**Revisit when.** The principal store becomes a network call per request rather than a local read — then cache the resolution with a short TTL, not the claims in the token.

## AUTH-02 — A member's work runs delegated, and is bound to their record in one place · **Locked**

**Fork.** Members are not Meridian operators — the back office has no member login — but a member console has to reach Meridian somehow.

**Options.**
- **Delegate**: execute as the deployment's least-privileged staff alias, and bind every member-identifying argument to the signed-in member.
- **Give members their own Meridian credential** — inventing an identity the target does not have.
- **A separate read-only member API** — a second implementation of the same lookups, which would drift from the recorded capabilities.

**Chosen.** Delegate, with `auth.scope_params` as the single binding point, and a role gate (`allowed_principal_roles`) that is asked about the SIGN-IN rather than the alias.

**Why / cost.** Delegation without scoping is a privilege grant: acting as `teller1` would otherwise hand every member a teller's action space. So the two are deliberately separate checks that must both pass, and the scoping rule is written once — fill in their own number, refuse a different one, refuse any share id or bare six-digit value that is not theirs — rather than per capability. The last clause is the catch-all: a parameter added tomorrow, which nobody thought to list, cannot smuggle another member's number through. Cost: a member's run is attributed to a teller alias in the target's own audit log, so the console records the subject beside the evidence (`principal.json`) to close that gap on our side.

**Revisit when.** The target grows a real member-facing session. Then a member's work should run as them, and the delegation disappears — but `scope_params` stays, because it is what makes the console's own projection safe.

## AUTH-03 — Refusing a cross-member request, never retargeting it · **Locked**

**Fork.** A member signed in as `100234` asks to transfer to `100987-S0001`. The scoped value is knowable — should the system correct it?

**Options.**
- **Refuse** with `MEMBER_SCOPE_VIOLATION`, naming the parameter.
- **Rewrite** to their own account and proceed.
- **Drop the parameter** and ask for it again.

**Chosen.** Refuse.

**Why / cost.** Rewriting would move money to a place the person did not ask for and report success, which is worse than any refusal. Dropping and re-asking is nearly as bad on a transfer: the reply reads as though nothing was supplied, so the same value gets retyped. Cost: a member who mistypes their own number gets an error rather than a correction, which is the right way round.

**Revisit when.** Never.

## AUTH-04 — The console narrows on the server, and a member gets a different page · **Locked**

**Fork.** One dashboard serves staff and members. How does a member see only their own?

**Options.**
- **Filter before serialising**, and serve members a purpose-built page.
- **One page, panels hidden in the browser** — every row still fetched.
- **A second application** — a fork of the dashboard that would drift.

**Chosen.** Filter server-side; `_MEMBER_PAGE` for members; every route that takes a run id re-checks ownership rather than trusting that the id came from a list the caller was allowed to see.

**Why / cost.** Hiding in CSS is one "view source" away from disclosure, and run ids are guessable — a capability name and a timestamp — on a host that serves screenshots of member accounts. A run with **no** recorded subject is treated as not theirs, which fails closed on exactly the operator-driven runs. Cost: two page templates to keep in step; the tests assert the member page never contains the operator panels.

**Revisit when.** A third audience appears. Two templates is fine; four is a signal to render from a shared description of what each role may see.

## AUTH-13 — The sign-on happens at the door, and then stops being askable · **Locked**

**Fork.** Every capability signs on for itself, and `meridian.signon` was also a capability in its own right — published in the manifest, invocable by anyone signed in. So a person at the console, or a model routing for them, could ask the system to sign on. What is that request supposed to mean once there is a session behind it?

**Options.**
- **Establish it at sign-in and withhold it afterwards**: the console runs the capability once, on the alias the sign-in names, through a purpose-built endpoint; for any caller carrying a session it is dropped from the manifest and refused at `/invoke`.
- **Leave it an ordinary capability** and let people re-run it. Simplest, and what shipped.
- **Delete it from the catalog** and let the per-capability sign-on steps be the only sign-on.

**Chosen.** Establish it at sign-in (`session_signon` in `config/service.yaml`, `POST /session/signon`), and withhold it from every signed-in caller.

**Why / cost.** The manifest is a chatbot's entire action space, and a tool whose only remaining effect is to redo what signing in already did is a tool that can only ever be called by mistake — a live browser driven against a shared host for nothing, or worse, whoever is in front of the console exercising an operator credential as an action of its own. Withholding it is only defensible because the sign-on genuinely happens: it runs down the same path as any invocation — same policy engine, same evidence directory, same result contract — so the run is in the history like everything else, and the approval gate still applies to it. Deleting it from the catalog was rejected for the opposite reason: nobody has signed in on the direct agent path, so there the capability is an ordinary one and still means something.

Two halves, and each is worthless without the other. The manifest narrowing is defence in depth; the refusal at `/invoke` is the defence, because a caller that never read the manifest gets the same answer.

**Revisit when.** Sessions are reused across capabilities (§5 of ADAPTATION's next-steps list). Then the sign-on establishes something that *outlives* the invocation, and where it happens stops being a presentational question.

## AUTH-14 — A host rejection stops the sign-in; an unreachable host does not · **Locked**

**Fork.** The sign-on at the door can come back three ways. Which of them should refuse the console sign-in?

**Options.**
- **Only a business outcome refuses.** The host answered about this credential and said no. A refusal or a failure means the sign-on was not *attempted* — API down, target offline, capability still in draft — so the person is signed in and the header says `MERIDIAN not verified`.
- **Anything but success refuses.** Fail closed, uniformly.
- **Nothing refuses**; always sign in, always show the verdict.

**Chosen.** Only a business outcome refuses.

**Why / cost.** This is the result contract's own distinction applied to the door: a business outcome is the bank answering, and a failure is something breaking. Failing closed uniformly would mean an offline target locks every operator out of a console whose authorisation does not depend on that target at all — the principal store, the role gate and the member scoping are all local. Signing everyone in regardless would let a definite "that credential is wrong" become somebody's first confusing capability failure instead. Cost: a person can be signed in to a console whose operator is not signed on to anything, which is why the state is on screen rather than inferred.

**Revisit when.** The console starts holding a target session rather than proving one. Then "not verified" is not a warning, it is a broken feature.

## AUTH-18 — A pause is shown where the person who caused it is sitting · **Locked**

**Fork.** A run started from the chatbot stops for a confirmation. The question — "may I post this?" — was raised on the dashboard's operator queue, a different tab, while the chat request sat on an open connection with nothing on screen. Where should it appear?

**Options.**
- **Stamp the surface on the intervention** (`channel`), let the assistant poll for its own while a call is in flight, and drop the ones it can answer from the operator queue.
- **Leave it on the dashboard only** and tell people to look there. Zero code; also the reason it looked broken.
- **Show it in both, always.** No new field, but the same irreversible step is then in front of two people.

**Chosen.** Stamp the channel; the assistant renders and resolves its own; the dashboard hides exactly those.

**Why / cost.** A person who asks a chatbot to move money is watching the chat. The answer existed and was reachable — it just was not anywhere they were looking, which is indistinguishable from a hung request. Showing it in both was rejected for a sharper reason than duplication: the person who did NOT ask has no context for what they would be approving, and an approval given without context is the rubber stamp the pause exists to prevent. Cost: two fields on the handoff record, and a poll loop that runs only while a request is open.

**Revisit when.** A third surface appears, or pauses need to outlive the request that raised them. Both would push this from a poll toward something the server pushes.

## AUTH-19 — Hiding a pause is only safe when its own surface can clear it · **Locked**

**Fork.** Having decided the assistant owns its pauses, which ones does the dashboard stop showing?

**Options.**
- **Only the ones answerable where they were raised**: a confirmation, raised in the assistant, by a supervisor.
- **All assistant-raised pauses** — the simple reading of "show it in the chat, not the dashboard".
- **None** — keep the queue complete and accept the duplicate.

**Chosen.** Only the ones answerable there, on both counts: `channel == "assistant"` AND `kind == risky_confirmation` AND `initiator_role == "supervisor"`.

**Why / cost.** The simple version has a hole that fails closed in the worst direction. A TELLER can raise a confirmation from the chat, but only a supervisor may clear one — hide it and it waits in a window whose occupant is not allowed to answer, then times out. A dual-control pause is worse: it asks for a *second* person by definition, so the surface that raised it can never be the one that clears it. Both stay in the queue. Cost: the rule has three clauses instead of one, so it is written in a single named function with the reason attached, and asserted directly (`test_the_hiding_rule_states_both_halves`).

**Revisit when.** Roles stop deciding who may confirm — then the condition is about capability, not role, and should be asked of the API rather than derived from a stamped field.

## AUTH-20 — The assistant has no operator picker · **Locked**

**Fork.** The chat page carried a `teller1 / super1` dropdown from the single-operator demo. With a sign-in in front of it, what is that control?

**Options.**
- **Delete it.** Signed in, the API derives the alias from the session; unattended, the deployment sets one with `chat --operator`.
- **Keep it, hidden by script when signed in** — what it did, and the reason it survived this long.
- **Keep it and honour it** — the caller choosing which operator to be, which is the exact escalation the credential store exists to close.

**Chosen.** Delete it.

**Why / cost.** With a session it was ignored, and naming the other operator was refused (`OPERATOR_NOT_SESSION`) — so the control could only ever do nothing or produce an error, and a control that behaves like that reads as a broken console rather than as a guardrail working. Hiding it in the browser left the same choice one "view source" away and kept a dead branch in the request path. Cost: the unauthenticated demo path loses a way to switch operator without restarting; `chat --operator` is that knob now, and it belongs to whoever runs the process rather than whoever types in it.

**Revisit when.** Never, while identity is established at sign-in.

## SAFE-15 — No irreversible step runs unattended, whatever the value policy allows · **Locked**

**Fork.** `approve_meridian.py` put three capabilities in an `UNATTENDED` set, ratifying their posting steps with `requires_confirmation: false` on the argument that the value policy — amount ceiling, dual-control threshold, rolling-window budget — is envelope enough. Is it?

**Options.**
- **Empty the set.** Every irreversible step stops for a person, and the value policy bounds what a mistake can cost on top of that.
- **Keep it**, and rely on the envelope.
- **Keep it but shrink it** to the flows with the tightest limits.

**Chosen.** Empty it.

**Why / cost.** The limits bound the SIZE of a mistake, not whether one is being made: a $1.00 transfer into the wrong share is inside every rule and is still wrong, and the host offers no reversal. The setting also has to agree with `config/service.yaml`, and when it did not the two combined into the one state the policy engine refuses outright — `requires_confirmation: false` with `allow_risky: false` is a hard BLOCK, not a pause — so the capability could not run at all and the console's Confirm button had nothing to answer. That was found by running it, not by reading it. Cost: no money moves without somebody present, which is the point and also the limit of this deployment's unattended usefulness.

**Revisit when.** A capability exists whose irreversible step is genuinely idempotent and verifiable after the fact (OPEN-07's confirm-then-retry probe). Then "did it land" can be asked instead of a person.

## Compact register — sign-in

| ID | Decision | Alternative rejected | Why |
|---|---|---|---|
| AUTH-05 | The published manifest is narrowed by the sign-in | Publish everything, refuse on invoke | The manifest IS a chatbot's action space; a capability a member may not use is best not described to the model routing for them. The API still refuses independently |
| AUTH-06 | A member's manifest omits `member_id` | Leave it and scope it | An argument the session supplies is an argument a model should not be invited to fill in with somebody else's number — the same rule as LIVE-03 |
| AUTH-07 | A signed-in caller cannot name an operator | Honour the request's alias when it resolves | A teller's session naming `super1` is precisely the escalation the credential store closed one layer down |
| AUTH-08 | Unlisted capabilities default to staff only | Default open to every signed-in role | The capabilities people forget to configure are the new ones. Failing closed against the public is the only default worth having |
| AUTH-09 | Both tabs on one origin, session in an `HttpOnly` cookie | A tab per port, token passed between them | A token in a URL is a token in an access log, a referrer header, and any screenshot of the address bar |
| AUTH-10 | Wrong password and unknown sign-in answer identically, and both are throttled | Say which | Member usernames ARE member numbers, so distinguishing them enumerates the membership |
| AUTH-12 | A member sign-in may not send `approver` or `tenant` | Leave both caller-supplied, as they always were | `approver` clears dual control, and a member's work is *initiated* by the delegated teller alias — so naming a supervisor would look independent and authorise their own large transfer with nobody approving it. Caller-supplied was defensible while every caller was the institution |
| AUTH-15 | Staff sign in by picking an operator, with no password | Keep the password field | A demo affordance for the automated sign-on, and a real hole written down in three places rather than hidden: anyone reaching the port becomes either operator. Nothing behind the door relies on it — the API still decides everything against the principal store, and a supplied password is still verified |
| AUTH-16 | Members keep their password | Put them in the list too | Member usernames ARE member numbers, so a list to pick from would be an enumeration of the membership with a login attached to each row |
| LIVE-16 | "The request could not be validated" is a business outcome, not a failure | Leave it unclassified | The host answering "your opening deposit is below the $5.00 minimum" surfaced as ACTION_FAILED, because the validation page replaced the form and the next step could not find its button. A caller's typo was paging an engineer |
| LIVE-17 | A reviewer downgrade that matches no step aborts the approval run | Skip it quietly, as it did | A re-recording renamed "Start a Funds Transfer" to "Open Funds Transfer form"; the downgrade silently did not apply, and a navigation-only step was approved as irreversible carrying a justification about posting money. A reviewer decision that evaporates when a caption changes is not a decision |
| AUTH-21 | `channel` is validated against a known set, not taken as sent | Accept whatever the caller sends | It decides which surface polls for the pause; an unknown value raises an intervention no window is watching, which is worse than not stamping one at all |
| AUTH-22 | The assistant proxies the pause endpoints rather than reading the handoff store | Read the store directly, as the dashboard does | The chatbot's whole defensibility is that it reaches the system only over the API and holds no state of its own — `tests/test_chat.py` asserts it imports nothing from `bankcua` |
| AUTH-17 | The sign-on verdict is a pointer to a run, held in process | Persist it; or re-derive it per request | It names the run id and what that run answered; the evidence the engine wrote stays the only account. A verdict about a session must not outlive the session, so signing out drops it |
| AUTH-11 | `require_session` is off by default in `config/service.yaml`, and `portal` turns it on | Require sessions everywhere | Non-browser callers — every CLI demo in the README — name an operator alias and cannot perform a browser sign-in. The browser stack always enforces it |

## DC-01 — A dual-control pause asks for a signature, not for the screen · **Locked**

**Fork.** Two different things stop a run: a gated *irreversible step*, and a *dual-control threshold*. Both were routed through the same intervention and the same co-browsing console.

**Options.**
- **Separate them**: a step pause hands over the live session; a threshold pause asks a second person to counter-sign, and records who.
- **Keep one path** — the console's "Resume automation" doubles as approval.
- **Refuse unattended and require the `approver` field on the invocation** — no console path at all.

**Chosen.** Separate them, with the identity taken from the console's sign-in and the independence check re-run in the engine.

**Why / cost.** Found by driving the demo, not by a test. A $2,000 transfer paused, and the dashboard offered "Take control of this session" — but the value check runs *before* the browser is sent anywhere, so `state_url` was `about:blank`, the screenshot was a broken image, and the operator got a blank page whose only exit was aborting the run. The result then came back `escalated / expected a second reviewer to counter-sign / observed resolved`, which reads like the system contradicting itself.

The deeper problem was underneath the UI: the engine resumed on `resolved` **alone**. Nothing recorded *who* resolved it, so a counter-signature was a click rather than an identity — and a click cannot be checked for independence. `InterventionRequest` now carries `initiator` and `resolved_by`, and `_satisfy_dual_control` re-checks `approver_is_independent` on the way back, so a console asserting "somebody clicked" is not the same as a ruling that they were allowed to. Cost: one more field on the handoff record, and the console must be signed in to counter-sign at all (the CLI path still takes an explicit `--approver`).

**Revisit when.** A second console exists that resolves interventions. The check is in the engine precisely so that day costs nothing.
