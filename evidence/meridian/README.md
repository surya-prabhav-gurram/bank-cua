# MERIDIAN CORE evidence

Live runs against `https://web-sample.interface-hiring.com`, one directory per
scenario. Regenerate with `python scripts/gen_meridian_evidence.py`.

Every scenario exercises a **different branch of the result contract**, because
that contract is what the system is for. A demo that only shows the happy path
proves the browser works; these show the system can tell a legitimate answer from
a guardrail decision from a fault, and reports each differently.

Each directory holds `summary.json` (the result contract), `run.jsonl` (the
redacted structured event log), and screenshots plus DOM snapshots where a step
captured them.

## Reads, and outcomes that are answers

| Scenario | Status | What it shows |
|---|---|---|
| `01-balances` | `success` | Typed outputs including `shares` as **rows** keyed on the grid's own headers — the one core change this target required. Note member `100234-S0001` comes back with status `HOLD`. |
| `02-member-search` | `success` | Candidate rows. Selects nothing: with several matches, choosing one is the caller's decision. |
| `03-no-such-member` | `business_outcome` | `NO_SEARCH_MATCHES`. Not a crash, not an error — the answer to the question asked. |

## Money movement, and the guardrails around it

| Scenario | Status | What it shows |
|---|---|---|
| `04-transfer-posted` | `success` | A real transfer posted through review→post, returning the host's confirmation number. The irreversible step ran because a human ratified it as bounded by the value policy (see `scripts/approve_meridian.py`). |
| `05-insufficient-funds` | `business_outcome` | `INSUFFICIENT_FUNDS`. Deliberately **under** the $1,000 dual-control threshold: at a higher amount our own gate refuses first and the run never reaches the host, which would demonstrate the wrong layer. |
| `06-over-ceiling-refused` | `refused` | `VALUE_LIMIT_EXCEEDED` with `steps_executed: 0`. The cheapest place to refuse an over-limit transfer is before anything is opened or typed. |

## A substring search is three different questions

MERIDIAN matches member numbers on **substring**, which gives three ways to be
uncertain about *who* was asked for. Answering any of them with somebody else's
balances is the worst outcome this system can produce, so all three are typed
answers that say what would resolve them — none is a failure.

| Scenario | Status | What it shows |
|---|---|---|
| `13-ambiguous-member` | `business_outcome` | `AMBIGUOUS_MEMBER_MATCH`. `100` matches two members. Detected by **counting** result rows, because a search matching one record and a search matching five render exactly the same words. |
| `14-member-number-not-exact` | `business_outcome` | `MEMBER_NUMBER_NOT_EXACT`. `1002` matches exactly one record — `100234` — so it is not ambiguous, and the flow selected it. No member `1002` exists, and this is the case that previously returned `status: success` with the wrong member's balances. |
| `03-no-such-member` | `business_outcome` | `NO_SEARCH_MATCHES`. Nothing matched at all. |

## Injected runtime conditions

| Scenario | Status | What it shows |
|---|---|---|
| `07-maintenance-recovered` | `success` | A maintenance interstitial forced on every request, cleared after 4s. Replay detects it, retries within its budget, re-scans, and completes — `recoveries` in `summary.json` names the condition. A *transient* window on purpose: holding it down permanently would only prove that a bounded retry gives up, which is scenario 08's job. |
| `08-session-expired` | `failure` | `SESSION_TIMED_OUT`. Deliberately **not** recoverable-by-re-authentication: we cannot know whether the step that timed out landed, and resuming across that boundary is where double-execution lives. The caller re-invokes with full knowledge; capabilities are stateless, so that is safe. |

## Authorisation, at both layers

The order these two fire in is the point.

| Scenario | Status | What it shows |
|---|---|---|
| `10-service-layer-refusal` | `refused` | `OPERATOR_NOT_PERMITTED`, HTTP 403, **no browser opened**. The request also carried `allow_risky: true` in its body; it was ignored, because authorisation is server-side in `config/service.yaml`. |
| `09-supervisor-required` | `business_outcome` | `SUPERVISOR_OVERRIDE_REQUIRED` — the *host's* own refusal, reached after five steps of a member's account had been walked. Relying only on this would mean the boundary holds only as long as the vendor keeps enforcing it. |

## Escalation and live-session handoff

| Scenario | Status | What it shows |
|---|---|---|
| `11-escalation-unattended` | `escalated` | The gated irreversible step raises an intervention carrying the capability, the step, the live URL, a screenshot and a **CDP endpoint**. Nobody answers, so it aborts cleanly and the request is preserved for triage. |
| `12-escalation-resolved` | `success` | The same pause, resolved: a supervisor attaches to **that exact live page** over CDP, applies the hold, and hands control back; replay resumes and returns the confirmation number. `evidence/handoffs/replay-meridian.place_hold-step9.json` holds the recorded `human_actions` and the final `controller: agent`. |

## The rest of the function surface

Replay evidence for the three capabilities that previously had only a discovery
run behind them. Replay is the production path, so a capability nobody has
watched replay is a capability nobody has watched work the way it will run.

| Scenario | Status | What it shows |
|---|---|---|
| `15-signon` | `success` | A session established and nothing returned — capabilities are stateless, so the verdict is the whole answer. |
| `16-open-share-posted` | `success` | Review→post on a second irreversible flow, returning the host's confirmation number. |
| `17-contact-details-updated` | `success` | Email, phone and address written to a member's record. |

## The injected fault kinds, one run each

The brief names six injectable kinds. Two were already covered (`maintenance` in
07, `timeout` in 08); these are the other four. `notfound` and `permission` each
have a NATURAL counterpart in the set already (03 and 09), so those two runs are
what prove the detectors match the injected rendering as well as the organic one.

| Scenario | Status | What it shows |
|---|---|---|
| `18-application-error-recovered` | `success` | `server` (HTTP 500) forced, cleared after 4s. `recoveries` names `APPLICATION_ERROR`: detected, reloaded within budget, re-scanned, carried on. |
| `19-validation-persistent` | `failure` | `validation` (HTTP 400) held down for the whole run. The other half of the recoverable contract — a bounded retry budget that is SPENT, so the run stops instead of looping. A retry policy with no exhaustion path is an infinite loop with good manners. |
| `20-notfound-injected` | `business_outcome` | `MEMBER_NOT_ON_FILE` from the injected 404, rather than 03's `NO_SEARCH_MATCHES` from a search that matched nothing. Two ways to be told a member is not there, typed apart. |
| `21-permission-injected` | `business_outcome` | `SUPERVISOR_OVERRIDE_REQUIRED` from the injected 403 — the same code 09 reaches organically. |

## Not committed

`value_ledger.jsonl` is regenerable run state, not evidence of a run — a
checked-in ledger would silently spend a reader's velocity budget before they ran
anything. The refusal `reason` in each summary carries the arithmetic instead.
