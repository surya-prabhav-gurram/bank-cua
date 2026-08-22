# Adaptation write-up — MERIDIAN CORE

The round-1 core is described in [REPORT.md](REPORT.md); every decision in both
rounds, with the alternatives that lost, is in
[docs/DECISIONS.md](docs/DECISIONS.md). This document answers the five questions
the adaptation brief asks.

---

## 1. What adapting actually took

**Recorded seven capabilities against MERIDIAN CORE — every one discovered by a
live model driving a live browser (`recorded_by: anthropic`) — with no change to
the schema, the compiler, the replay engine, the error taxonomy machinery, or the
safety model.** The changes that were needed are listed below — all of them, including
the ones that were bugs in the core rather than gaps.

### Adapted for free

The parts most likely to have broken did not.

- **Locators.** MERIDIAN's inputs have no `id`, no `label for`, no test IDs —
  `<td class="lbl">Operator ID:</td><td><input name="operator">`. That is exactly
  the shape `LocatorKind.NEAR_LABEL` was built for in round 1, and sign-on worked
  on the first attempt with zero new code.
- **review → post.** Structurally identical to round 1's sub-account flow. No new
  concept.
- **The per-transaction token.** See §3 — it required nothing, and that is a
  consequence of the design rather than luck.
- **Risk classification.** Caught all four irreversible steps. Three of the four
  (`Open Share`, `Apply Hold`, `Save Changes`) carry no risky keyword and were
  caught **only** by the structural POST-form signal. A lexical-only classifier
  would have missed three of four irreversible actions on this target.

### Changes to the core, and why each was necessary

| Change | Why |
|---|---|
| Boundary-aware checkpoint matching | **The most serious defect found.** MERIDIAN's member search matches on SUBSTRING, so a lookup for member `1002` selected member `100234`, and the step's checkpoint — `url_matches: /members/1002` — passed by plain containment. The run reported `success` and returned a different member's balances. Not an error: an *answer*, about the wrong person. Matching now requires a boundary, the same rule redaction already used for secrets. |
| Three typed outcomes for an uncertain member number | Failing hard was safe but wrongly typed. Several records matching, or a partial number landing on a different member, are QUESTIONS needing more information — so they are `business_outcome`s that say what would resolve them, alongside the existing "nothing matched". Needed parameterised detectors, a counting detector, URL scoping, and compound conditions. |
| A failed action never reaches the transcript | The invariant the design rests on is that a recorded step already worked. A scripted run compiled a `select` that had **timed out** and still reported SUCCESS, because the final checkpoint happened to pass anyway — the capability shipped containing a step that had never worked. Now the loop corrects and retries, and the compiler refuses a transcript containing a failure. |
| Text controls report emptiness | A live model filled the same search box four times and was stopped by the stuck detector: we never expose a control's typed value, so a filled box and an empty one were indistinguishable. Text fields now report `empty` / `has a value` — enough to know a step is done, carrying no part of the secret. |
| `select_option` resolves by either handle | MERIDIAN's option labels read `"100234-S0070 - Share Draft ($226.55)"` over a bare value. Whether a recording names the value or the label is a property of the markup, not the intent — and Playwright answers the mismatch with an 8-second timeout rather than a useful error. |
| The compiler binds decorated option labels to parameters | A live recording carried `from_share: "100234-S0070 - Share Draft (Checking) ($232.55)"` as a **literal** — a balance welded into the artifact, unable to ever replay. Prefix-matching on a boundary binds it to `{from_share}` instead. |
| The manifest omits credentials and service-supplied identity | Given the unfiltered manifest, a live model **stopped and asked the user for the operator's password** rather than calling the tool. That is the manifest working exactly as written and exactly wrong: no caller should hold a credential. |
| `attribute="table"` + `ValueType.TABLE` + `ActResult.rows` | Balances live in a grid whose row count varies per member. No fixed set of scalar extracts can express that. Reading a grid is a *perception* concern, so it rides the existing `attribute` field rather than becoming a new extraction path — two enum extensions, no new `LocatorKind`, no new step type. |
| Label/value adjacency | MERIDIAN packs **two** label/value pairs per row. Round 1 paired the first cell with the *last*, so `E-mail:` returned the phone number. Now each label pairs with the cell beside it. A silent, plausible-looking mis-read. |
| Action-scoped conditions (`applies_to_actions`) | MERIDIAN renders `TRANSACTION REJECTED` both for a genuine validation failure *and* when its fault injector fires on an ordinary page load. Same text, opposite handling. Classifying on text alone must be wrong half the time. |
| No recovery on irreversible steps | A recovery is a retry. With a 20% injected fault rate on posting actions, retrying a posted transfer is how one transfer becomes two. Now refused by default (`allow_recovery_on_risky_steps`). |
| Stuck detection rewritten | The old rule escalated whenever four steps shared a URL — so *every* MERIDIAN flow with a form wider than three fields escalated instead of recording. Now keyed on repetition of `(url, action, target)`, which catches a dead control *and* a model spinning on one field, while treating filling five fields as the progress it is. |
| Vendor taxonomies moved to YAML | Adding MERIDIAN's ten conditions became a config file, not a patch. The people who know a vendor's screens are not the people who know this engine. |
| `catalog refresh-conditions` | The taxonomy is copied into an artifact at compile time, so an approved capability's behaviour cannot change because someone edited a shared file. That is correct — and it needs a governed path to propagate a fix: bump the version, land in `draft`, let the approval gate decide. |

### Where the core was too coupled

Two places, honestly:

1. **The stuck heuristic encoded an assumption about the shape of a flow** —
   that progress means navigation. That held for round 1's three-field mock and
   was wrong on the first real target with a wide form.
2. **`index_readouts` assumed one label/value pair per row.** Also true of the
   round-1 mock, also false here. Both were written against one target and
   generalised on no evidence.

Neither cost a rewrite, but both are the same mistake: a heuristic validated
against a single surface. Building the second surface in round 1 caught this
class of error once already (it is why `NEAR_LABEL` exists); this target caught
two more.

---

## 2. The capability API and its contract

Seven capabilities cover §2.1's seven functions:

| Capability | Covers | Returns |
|---|---|---|
| `meridian.signon` | Sign on / session | *(nothing — see below)* |
| `meridian.member_search` | Inquiry by last name | `matches` (rows) |
| `meridian.member_lookup` | Inquiry by number, record, balances | `member_name`, `email`, `phone`, `shares` (rows) |
| `meridian.transfer_funds` | Funds transfer | `confirmation` |
| `meridian.open_share` | Open new share | `confirmation` |
| `meridian.update_member_info` | Update member information | `email`, `phone` |
| `meridian.place_hold` | Place account hold | `confirmation` |

Four contract decisions worth defending:

**`signon` is established at the door, not requested.** Every capability signs
on for itself, so a *separate* sign-on capability, published to a signed-in
caller, can only ever be called by mistake: the session it would establish is the
one the caller is already holding. The console therefore runs it once when a
person signs in — on the alias their sign-in names, down the same engine, policy
and evidence path as any invocation — and the API then withholds it from the
manifest and refuses it at `/invoke` for that session
(`SIGNON_ESTABLISHED_AT_SIGN_IN`). The model in front of the console cannot route
to it, because it is not in the action space at all. On the direct agent path,
where nobody has signed in, it stays an ordinary capability.

**Capabilities are stateless.** Each replay signs on fresh, completes one
transaction, and returns. There is no session to hand between invocations — which
is why `signon` returns no data (a verdict is the whole answer) and why "Member
Inquiry / Selection" could not be a standalone capability that leaves a member
selected. The cost is one extra sign-on per call, and more exposure to the fault
rate. It buys independent auditability: every invocation is a complete
transaction with its own evidence.

**`member_search` selects nothing.** Both search modes land on a results list, so
a recorded "Select" click on a multi-row result would pick an arbitrary member.
Acting on the wrong member's account is the worst error this system could make,
so search returns candidates and the caller disambiguates.

**`update_member_info` returns the record, not the banner.** The host's
acknowledgement says a write was accepted; reading the values back off the member
record says it landed. Same reason a fill is verified by reading the control.

The API surface itself is in [`bankcua/service.py`](bankcua/service.py):
`GET /capabilities` (function-calling manifest), `GET /capabilities/<id>`,
`GET /operators`, `POST /invoke/<id>`, `GET /runs[/<id>]`.

---

## 3. Driving this UI reliably

**The per-transaction token needed no handling, and that is a design consequence.**
The review page carries the whole transaction as hidden inputs including
`_token`; because we drive a real browser and click a real submit control, the
browser sends all of it. We never read or replay the token.

That argument only holds if every state-changing submission is a native form
post, so it was checked rather than assumed: **zero JavaScript across all six
flows**, every submit a real `<input type="submit">`. Two further findings, since
a claim should be tested rather than asserted: the token is **session-scoped, not
per-transaction** (identical across the settings page, the transfer form and the
review page), and it is **not validated at the review step** — a corrupted or
absent token passes. Whether the post step validates it is the one verification
still open, noted in §5.

**The target is a shared, hostile environment.** It carries a configurable random
error rate on posting actions and a *global* forced-injection setting any visitor
can change. During development it was found at `errorRate=1.0` with a forced
`validation` inject, and later at forced `timeout` — every request failing, set by
someone else. So scenarios declare the fault state they need through
[`scripts/meridian_control.py`](scripts/meridian_control.py), and
`config/policy.meridian.yaml` **blocks `*/settings*` for the agent**: the harness
may set up the world, the automation may never change the conditions it is judged
under. An automation that can switch off its own error conditions can hide its
own failures.

**Detector precision needed real verification.** `SUPERVISOR OVERRIDE REQUIRED`
is printed as a standing *warning* on the hold form at HTTP 200 for every
operator including supervisors. Detecting on it told a supervisor they were not
authorised and turned every hold into a false business outcome. The real denial
says `is not authorized to perform this`. Found by running the same capability as
both roles — which is why the evidence set does exactly that (scenarios 09 and
11/12).

---

## 4. How the guarantees survive the new surface

**Who is asking is now part of the answer.** The three surfaces originally came
up open, with the operator picked from a dropdown — fine for one operator,
wrong the moment anyone else uses it. `bankcua portal` puts a sign-in in front
(supervisor, teller, and one per seed member) and mounts the dashboard and the
chatbot behind it. Staff pick their operator from a list and are **signed on to
MERIDIAN by the act of signing in** — no operator password is typed anywhere,
because the secret stays in the credential store and is merged in server-side —
and the sign-on capability then leaves the action space entirely. A host
rejection stops the sign-in with the host's own words; a host that cannot be
reached does not, because an unreachable target is not evidence about anybody's
credential. The staff sign-in takes no password of its own, which is a demo
affordance and a real hole, stated as one in the README and in the module that
implements it. The authorisation is **not** in the console: it mints a
signed token carrying a username and an expiry only, and the capability API
re-resolves it against the principal store on every call, applying the role gate
(`allowed_principal_roles` in `config/service.yaml`). Deleting the console would
cost the system its sign-in page and none of its authorisation. The console
admits the institution's own operators and nobody else — MERIDIAN is a back
office, its users are tellers and supervisors, and `PrincipalStore.get` refuses
any principal whose role is not an operator role, so that holds against a
hand-edited principal file and not merely against the forms on the page.
Authorisation asks two separate questions that must both pass: which role signed
in, and which Meridian operator the run executes as — `meridian.place_hold`
constrains both. Beyond the brief, and stated as such: it exists because §3.5
asks that the wrapper not become a way around the guardrails, and an
unauthenticated wrapper is the largest way around them.

The wrapper is where a safety model usually dies, so the load-bearing decisions
are all about what the caller *cannot* do.

**Authorisation is server-side.** The earlier service accepted `allow_risky` and
`allow_unapproved` **in the request body** and a password per call. With a
chatbot in front, that is a language model authorising funds transfers, and a
caller choosing which operator to be. All of it now lives in
[`config/service.yaml`](config/service.yaml): whether a capability may perform
its irreversible step, whether the approval gate applies, which role it requires,
and which aliases may invoke it. Nothing the caller sends is consulted when
deciding what it may do. Asserted by
`test_a_request_cannot_grant_itself_permission_for_irreversible_actions`.

**Credentials never cross the wire.** Callers name an operator *alias*; the
secret is resolved server-side by a `CredentialStore` and merged in *after* the
caller's params, so a param of the same name is overwritten rather than honoured.
This also closes round 1's real leak — `--param password=...` in shell history and
`ps` output.

**Two independent authorisation layers, and the order matters.** A teller placing
a hold is refused by the *service* on identity, with **zero steps executed**
(evidence 10). The *host* would also refuse — at its own screen, after five steps
of a member's account had been walked (evidence 09). Relying only on the host
means the boundary holds only as long as the vendor keeps enforcing it.

**Escalation survives.** The service launches each run with a CDP endpoint and a
handoff coordinator, so a gated step still pauses, exposes the live session, and
waits. Evidence 11 shows it timing out unattended; evidence 12 shows a supervisor
attaching to that exact page, applying the hold, handing control back, and replay
resuming to `SUCCESS` with a confirmation number.

**A pause is answered where it was raised.** A run started from the chatbot
stops for a human confirmation like any other, but the question used to appear
on the dashboard's operator queue while the person who asked sat in front of a
request that looked hung. The intervention now records which surface started
the run, the assistant polls for its own while the call is open and renders the
pause — reason, the live screen, a `Confirm and continue` button — into the
conversation, and the operator queue stops listing the ones it can answer.
Exactly the ones it cannot are kept there: a teller's confirmation, which needs
a supervisor, and a dual-control pause, which needs a second person by
definition. Hiding either would strand a run in a window whose occupant is not
allowed to clear it (`bankcua/dashboard.py::_answered_in_the_assistant`,
asserted in `tests/test_assistant_handoff.py`).

**The model's blast radius is the catalog.** The chatbot imports nothing from
`bankcua` except its own router and presenter — asserted structurally in
`test_the_chatbot_cannot_reach_past_the_api`. It chooses *which typed capability*
to call and nothing else. A wrong routing decision produces a wrong question,
answered safely.

**The distinction survives into English.** The result contract separates four
things, and the chatbot is the only part most people will see. A business outcome
leads with what the bank said; a refusal says nothing was submitted *and what
would change it*; only a failure sounds like something is wrong. Enforced by
`test_the_four_statuses_never_render_identically`.

**The dashboard has no store.** Every row is derived from the evidence the engine
already wrote. `test_the_dashboard_never_writes` parses the module and fails on
any file opened for writing, because a second account of what happened would
eventually disagree with the first.

---

## 5. What I cut, and what I would do next

**Cut deliberately:**

- **The chatbot is deliberately thin.** It is a demo driver over the API, not a
  second product. `--router llm` routes with a live model over the published
  manifest; the default deterministic router needs no key. Both return the same
  object, and neither can reach past the API.
- **One gated run at a time.** The service exposes a single CDP port, so
  concurrent escalations would collide.
- **The console's identity store is a file, not an identity provider.**
  Sign-in is real and the authorisation built on it is enforced server-side
  (see §4), but `config/principals.json` is a gitignored JSON file with hashed
  passwords, and `PrincipalStore` is the seam an IdP replaces. No SSO, no MFA,
  no password rotation policy.
- **Screenshots are still unmasked.** Treated as sensitive evidence; the
  production answer is masked capture plus a restricted evidence store.

**Next, in order:**

0. **Rotate the demo API key**, which was shared over chat during development.

1. **Finish the token verification.** Confirm whether the *post* step validates
   `_token` — the review step demonstrably does not. It changes nothing about the
   design and everything about what we are entitled to claim.
2. **A real identity provider behind `PrincipalStore`.** Sign-in now
   establishes the subject (§4), but `approver` is still a self-asserted string
   checked against a configured registry rather than a second person actually
   authenticating — so dual control proves independence of NAME, not of PERSON.
3. **Confirm-then-retry on irreversible steps.** Today a fault after a post is a
   hard failure requiring human reconciliation, which is safe but blunt. The
   right answer is to check whether the post landed — a per-capability "how do I
   tell" probe — and that deserves its own design pass.
4. **A real OS-level driver** behind `_ax_nodes()`, replacing the last stand-in
   in the surface story.
5. **Session reuse across capabilities**, behind a seam, once the cost of one
   sign-on per invocation is measured rather than assumed.
