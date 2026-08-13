# bank-cua — record-once / replay-many computer use for legacy back-office apps

An LLM figures out how to accomplish a task in a legacy UI **once** (discovery),
the run is compiled into a **typed, versioned, reviewable capability artifact**,
and that artifact is then **replayed deterministically with no model in the
loop** — the path an AI agent triggers in production. Replay handles real
runtime conditions (validation errors, not-found, permission denials,
interstitials, timeouts), enforces safety guardrails, and escalates to a human
who can take control of the *same live session* when it can't safely proceed.

> The model discovers → the artifact becomes a reusable capability →
> deterministic replay is how the agent invokes it in production.

See **[REPORT.md](REPORT.md)** for the design write-up and trade-offs.

---

## What's here

| Area | Where |
|---|---|
| Typed capability artifact schema | `bankcua/schema.py` |
| Surface abstraction (perceive/act seam) | `bankcua/surface/base.py` |
| Playwright web surface (frames, locator synthesis, CDP) | `bankcua/surface/web_playwright.py` |
| Goal-driven discovery loop + LLM providers | `bankcua/agent/` |
| Transcript → artifact compiler | `bankcua/agent/compiler.py` |
| Deterministic replay engine + error taxonomy | `bankcua/replay/` |
| Locator robustness (label-proximity) + drift telemetry | `surface/web_playwright.py`, `replay/result.py` |
| Bounded, policy-checked assisted recovery | `bankcua/replay/engine.py` |
| Safety: allowlist, risk, redaction | `bankcua/safety/` |
| Escalation & live-session handoff (CDP) | `bankcua/escalation/handoff.py` |
| Vendor-shared known-condition library | `bankcua/knowledge.py` |
| Cross-tenant reuse: overrides + canonicalization | `bankcua/tenancy.py` |
| Capability catalog + agent-facing HTTP API | `bankcua/catalog.py`, `bankcua/service.py` |
| Code generation (artifact → Playwright script) | `bankcua/codegen.py` |
| CLI | `bankcua/cli.py` |
| Mock legacy bank app (proxy target) | `mockbank/app.py` |
| Saved artifacts | `capabilities/` |
| Discovery + replay + handoff evidence | `evidence/` |

## The proxy target

`mockbank/` is a deliberately **legacy-style** web app: server-rendered,
table-based markup, **no test IDs**, the savings balance rendered inside an
**iframe**, and cookie sessions that expire. It exposes deterministic,
injectable exception states so replay error-handling is testable without
flakiness — see the docstring in `mockbank/app.py`.
Login (fake, demo only): `operator` / `password123`.

---

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate     # optional
pip install -r requirements.txt
# Chromium for Playwright (skip if PLAYWRIGHT_BROWSERS_PATH already provides it):
python -m playwright install chromium
```

`ANTHROPIC_API_KEY` is only needed to run a **fresh live discovery** with the
real API provider. Replay and the offline discovery reproduction need no key.

---

## Verify everything (one command)

On a machine with internet (e.g. your Mac Terminal):

```bash
cd bank-cua
bash scripts/verify.sh
```

This creates an isolated venv, installs dependencies + Chromium, then runs the
full test suite, an offline discovery→artifact run (no key, into a scratch dir so
your committed artifacts are untouched), all replay/error/cross-tenant/handoff/
stability scenarios, the agent API + codegen, and safety sweeps — printing a
clear per-step PASS/FAIL and a final `VERIFICATION PASSED`. Takes a few minutes
(mostly dependency install).

---

## Demo path (exact commands)

Start the mock app in one terminal:

```bash
python mockbank/app.py            # serves http://127.0.0.1:5057
```

### 1. Discovery → capability artifact

Reproduce discovery offline from the recorded model decisions (no key):

```bash
bash scripts/run_discovery.sh
# -> capabilities/corebank.member_savings_lookup.json
# -> capabilities/corebank.open_subaccount.json
# -> evidence/discovery-*/  (screenshots, transcript, run.jsonl, bridge_trace)
```

Or run a **genuinely live** discovery with your own key:

```bash
export ANTHROPIC_API_KEY=sk-...
python -m bankcua.cli discover \
  --task config/tasks/member_savings_lookup.json --provider anthropic
```

Inspect the capability catalog:

```bash
python -m bankcua.cli catalog list
python -m bankcua.cli catalog manifest        # agent-callable function schema
python -m bankcua.cli catalog show --id corebank.member_savings_lookup
```

### 2. Deterministic replay (no LLM) — the production path

```bash
# happy path -> success + typed outputs
python -m bankcua.cli replay \
  --artifact capabilities/corebank.member_savings_lookup.json \
  --param username=operator --param password=password123 --param member_id=12345

# business outcome (NOT a crash): no such member
python -m bankcua.cli replay \
  --artifact capabilities/corebank.member_savings_lookup.json \
  --param username=operator --param password=password123 --param member_id=00000
```

Result is a structured contract distinguishing `success` (+outputs),
`business_outcome` (e.g. `MEMBER_NOT_FOUND`), and `failure`
(with step, expected, observed, and evidence).

### 3. Inject runtime conditions

```bash
# recoverable: an unexpected maintenance interstitial is auto-dismissed
curl "http://127.0.0.1:5057/_control/set?key=inject&value=interstitial"
python -m bankcua.cli replay --artifact capabilities/corebank.member_savings_lookup.json \
  --param username=operator --param password=password123 --param member_id=12345
curl "http://127.0.0.1:5057/_control/reset"

# hard failure: session timeout is detected and surfaced (not blindly retried)
curl "http://127.0.0.1:5057/_control/set?key=timeout&value=on"
python -m bankcua.cli replay --artifact capabilities/corebank.member_savings_lookup.json \
  --param username=operator --param password=password123 --param member_id=12345
curl "http://127.0.0.1:5057/_control/reset"
```

### 4. Escalation & human handoff on the same live session

Replaying the sub-account capability hits an **irreversible** "Confirm and
Create" step. Without risk approval, replay pauses and raises an intervention;
a human operator takes control of the **same live browser over CDP**, performs
the step, and hands control back — then replay resumes.

Terminal A (replay pauses at the gated step, exposes the session on CDP):

```bash
python -m bankcua.cli replay \
  --artifact capabilities/corebank.open_subaccount.json --cdp-port 9222 \
  --param username=operator --param password=password123 \
  --param member_id=12345 --param acct_type="Money Market" --param deposit=500.00
```

Terminal B (operator console stand-in — list, then take control & resolve):

```bash
python -m bankcua.cli operator list
python -m bankcua.cli operator resolve --id replay-corebank.open_subaccount-step9 \
  --do "click_selector:input[type=submit]" --manual
```

A self-contained, scripted version of this handoff is in
`scripts/gen_evidence.py` (scenario 06) if you don't want two terminals.

### 5. Cross-tenant reuse (one artifact, another institution)

The same capability, recorded on tenant `demo-cu`, runs against tenant
`summit-cu` — the same vendor product with rebranded labels ("Member Number" vs.
"Member ID", "Find" vs. "Search") — via a ~4-line string override. Start the
Summit variant, then:

```bash
MOCKBANK_PORT=5059 MOCKBANK_VARIANT=summit python mockbank/app.py   # other terminal

# clean run on Summit using the tenant override
python -m bankcua.cli replay --artifact capabilities/corebank.member_savings_lookup.json \
  --tenant config/tenants/summit-cu.json \
  --param username=operator --param password=password123 --param member_id=12345
```

With no override, the same artifact still succeeds on Summit via structural
locator fallbacks and reports `drifts` — graceful degradation, not a cliff.

### 6. Confidence: stability signal + approval gate

```bash
# replay N times -> pass rate; optionally write it into the artifact
python -m bankcua.cli replay --artifact capabilities/corebank.member_savings_lookup.json \
  --repeat 5 --update-stability \
  --param username=operator --param password=password123 --param member_id=12345

# unattended replay is refused until a human approves the capability
python -m bankcua.cli replay --artifact capabilities/corebank.member_savings_lookup.json \
  --require-approved --param username=operator --param password=password123 --param member_id=12345
python -m bankcua.cli catalog approve --id corebank.member_savings_lookup
```

### 7. Agent-facing API + code generation

```bash
# expose capabilities as callable-by-name HTTP endpoints
python -m bankcua.cli serve            # http://127.0.0.1:8080
#   GET  /capabilities            -> function-calling manifest
#   POST /invoke/<id>             -> runs replay, returns the result contract

# emit a runnable standalone Playwright script from an artifact
python -m bankcua.cli codegen --artifact capabilities/corebank.member_savings_lookup.json \
  --out generated/lookup.py
```

Assisted recovery (opt-in, bounded): add `--assist` to a replay to allow one
policy-checked LLM decision to recover a single failed step (never open-ended).

### 8. Regenerate all evidence at once

```bash
python scripts/gen_evidence.py     # starts both tenants; runs scenarios 01–09
```

---

## Tests

```bash
python -m pytest -q
```

Unit tests (schema, policy, redaction, compiler, transforms) run without a
browser; integration tests start the mock app and exercise the replay result
contract end-to-end (they skip if Playwright/the app can't start).

---

## Notes / what is mocked

- **The committed discovery evidence is a genuine live run** against the mock via
  the Anthropic Messages API (`AnthropicProvider`) —
  see `evidence/discovery-*-anthropic-live/` (`recorded_by: anthropic`, per-step
  screenshots + transcript). A **key-free offline reproduction** of an equivalent
  run is also provided via the `bridge` provider (`scripts/run_discovery.sh`),
  which drives the same real loop from a recorded decision trace.
- **Operator console** is a CLI stand-in; the **handoff mechanism is real**
  (CDP attach to the live session, recorded human actions, control token,
  resume). See REPORT §5.
- **Cross-tenant reuse is demonstrated** end-to-end against a second rebranded
  tenant variant (`MOCKBANK_VARIANT=summit`). The **desktop/legacy-web surfaces**
  remain designed, not built — see REPORT §4. The `Surface` seam keeps them out
  of the core's way.
