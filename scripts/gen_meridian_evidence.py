#!/usr/bin/env python3
"""
Evidence for the Meridian adaptation: one directory per scenario, run live.

Every scenario below exercises a different branch of the result contract, because
that contract is the thing the system is actually for. A demo that only shows the
happy path proves the browser works; these show that the system can tell a
legitimate answer from a guardrail decision from a fault, and reports each
differently.

  01 balances read (typed rows)          08 hard fault: session expiry mid-flow
  02 member search                       09 supervisor-only, as a teller (outcome)
  03 no such member (outcome)            10 supervisor-only, refused by the SERVICE
  04 funds transfer posted               11 escalation: gated step, no operator
  05 overdraw (outcome)                  12 escalation RESOLVED on the live session
  06 amount over the ceiling (refused)   13 several members matched (outcome)
  07 fault injected, recovered           14 partial number, wrong member (outcome)

  15 sign-on (session established)       19 validation fault held down (retries give up)
  16 new share opened (review->post)     20 injected 404: member not on file (outcome)
  17 contact details updated             21 injected 403: supervisor override (outcome)
  18 injected 500, recovered

Scenarios 15-21 exist because the brief names six injectable fault kinds and a
seven-function surface, and evidence that stops short of either is a claim about
coverage rather than a demonstration of it. 15-17 are the three capabilities that
had discovery evidence but no REPLAY evidence; 18-21 are the four inject kinds
(`server`, `validation`, `notfound`, `permission`) that the condition library
declares detectors for but no live run had exercised.

Scenarios that need the target in a particular fault state set it explicitly
through scripts/meridian_control.py, then restore the baseline -- the host is
shared, and a scenario that assumes a clean target just reports whatever the last
visitor left behind.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))
os.chdir(os.path.abspath(os.path.dirname(__file__) + "/.."))

from meridian_control import TargetControl                        # noqa: E402

from bankcua.catalog import Catalog                                # noqa: E402
from bankcua.escalation.handoff import (HandoffCoordinator,        # noqa: E402
                                        HandoffStore, OperatorSession)
from bankcua.observability.logging import RunLogger                # noqa: E402
from bankcua.replay.engine import ReplayEngine                     # noqa: E402
from bankcua.safety.credentials import EnvCredentialStore          # noqa: E402
from bankcua.safety.ledger import Ledger                           # noqa: E402
from bankcua.safety.policy import Policy, PolicyEngine             # noqa: E402
from bankcua.surface.web_playwright import WebSurface              # noqa: E402

ROOT = "evidence/meridian"
POLICY = Policy.from_yaml("config/policy.meridian.yaml")
CAT = Catalog("capabilities/meridian")
CREDS = EnvCredentialStore("config/credentials.json")
MEMBER = "100234"


def _params(operator: str, **kw) -> dict:
    identity = CREDS.resolve(operator)
    return {"operator": operator, **identity.context, **identity.secrets, **kw}


def run(tag: str, cap_id: str, operator: str, *, allow_risky=False,
        coordinator=None, coordinator_timeout=None, cdp_port=0, ledger=None,
        approver="", **kw):
    run_dir = os.path.join(ROOT, tag)
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    art = CAT.get(cap_id)
    params = _params(operator, **kw)
    logger = RunLogger(run_dir, "replay", art.secret_params(),
                       {n: str(params[n]) for n in art.secret_params()
                        if params.get(n)})
    pe = PolicyEngine(POLICY, artifact_url_patterns=art.target.allowed_url_patterns,
                      allow_risky_override=allow_risky)
    surf = WebSurface(art.target.base_url, headless=True, cdp_port=cdp_port)
    surf.start()
    # Built HERE, with the run's own logger: a coordinator wired to a different
    # logger writes the escalation into somebody else's run.jsonl, so the run
    # that actually paused has no record of pausing.
    if coordinator_timeout is not None:
        coordinator = HandoffCoordinator(HandoffStore("evidence/handoffs"), logger,
                                         wait_timeout_s=coordinator_timeout)
    res = None
    try:
        res = ReplayEngine(surf, pe, logger, coordinator, initiator=operator,
                           approver=approver, ledger=ledger).run(art, params)
    finally:
        logger.finish(json.loads(res.model_dump_json()) if res else {})
        surf.stop()
    _line(tag, operator, res)
    return res


def _line(tag: str, operator: str, res) -> None:
    bits = [f"[{tag}] as {operator}: {res.status.value}"]
    for holder in (res.business_outcome, res.refusal, res.failure):
        if holder is not None:
            bits.append(holder.code)
    if res.outputs:
        shown = {k: (f"{len(v)} rows" if isinstance(v, list) else v)
                 for k, v in res.outputs.items()}
        bits.append(str(shown))
    if res.recoveries:
        bits.append("recovered=" + ",".join(r.condition_code for r in res.recoveries))
    if res.intervention_id:
        bits.append(f"intervention={res.intervention_id}")
    print("  " + "  ".join(bits))


def escalation_resolved():
    """12: the gated step is completed by a supervisor on the SAME live session.

    This is the round-1 handoff mechanism reached through the round-2 surface,
    unchanged: replay pauses on an irreversible step, exposes its browser over
    CDP, an operator attaches to that exact page, performs the action, and hands
    control back. The operator here is scripted so the scenario runs unattended;
    a person does the same thing through `bankcua.cli operator console`.
    """
    tag = "12-escalation-resolved"
    run_dir = os.path.join(ROOT, tag)
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    store = HandoffStore("evidence/handoffs")
    art = CAT.get("meridian.place_hold")
    # Derived, never hard-coded: the gated step's index moves whenever the
    # capability is re-recorded and the model finds a different path. A literal
    # index here silently waits on an intervention that is never raised, and the
    # scenario reports "escalated, no operator" -- which looks like the operator
    # declined rather than like the harness watching the wrong door.
    gated = next(st.index for st in art.steps if st.requires_confirmation)
    req_id = f"replay-{art.id}-step{gated}"
    logger = RunLogger(run_dir, "replay", art.secret_params(), {})

    def operator():
        for _ in range(120):
            try:
                req = store.read(req_id)
                if req.status.value == "open" and req.cdp_endpoint:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            return
        session = OperatorSession(store.read(req_id), store).attach()
        try:
            session.do("note", "supervisor reviewed the hold and is applying it")
            session.do("click_selector", "input[type=submit]")
            time.sleep(0.6)
            session.resolve(note="performed manually by supervisor", resume=True)
        finally:
            session.detach()

    thread = threading.Thread(target=operator)
    thread.start()
    pe = PolicyEngine(POLICY, artifact_url_patterns=art.target.allowed_url_patterns,
                      allow_risky_override=False)
    surf = WebSurface(art.target.base_url, headless=True, cdp_port=9223)
    surf.start()
    coordinator = HandoffCoordinator(store, logger, wait_timeout_s=90)
    res = None
    try:
        res = ReplayEngine(surf, pe, logger, coordinator, initiator="super1").run(
            art, _params("super1", member_id="100987", share_id="100987-S0001-3",
                         reason_code="LEGAL", notes="evidence scenario"))
    finally:
        thread.join(timeout=10)
        logger.finish(json.loads(res.model_dump_json()) if res else {})
        surf.stop()
    final = store.read(req_id)
    print(f"  [{tag}] as super1: {res.status.value}  "
          f"human_actions={len(final.human_actions)}  "
          f"controller={final.controller}  outputs={res.outputs}")


def _service_layer_refusal():
    """10: refused by the API before a browser is ever opened.

    Two independent layers say no to a teller placing a hold, and it matters
    which one speaks first. The SERVICE refuses on identity, so nothing is
    driven at all; the HOST would also refuse, at its own screen, after five
    steps of a member's account had been walked through (scenario 09). Relying
    only on the host would mean the boundary holds only as long as the vendor
    keeps enforcing it.
    """
    from bankcua.service import create_app
    run_dir = os.path.join(ROOT, "10-service-layer-refusal")
    os.makedirs(run_dir, exist_ok=True)
    app = create_app(catalog_dir="capabilities/meridian",
                     policy_path="config/policy.meridian.yaml",
                     service_config_path="config/service.yaml",
                     evidence_dir=os.path.join(run_dir, "runs"),
                     credential_store=CREDS)
    resp = app.test_client().post(
        "/invoke/meridian.place_hold",
        json={"operator": "teller1", "allow_risky": True,
              "params": {"member_id": MEMBER, "share_id": f"{MEMBER}-S0070",
                         "reason_code": "FRAUD", "notes": "evidence scenario"}})
    body = resp.get_json()
    with open(os.path.join(run_dir, "summary.json"), "w") as fh:
        json.dump({"http_status": resp.status_code,
                   "note": "allow_risky was sent in the request body and ignored; "
                           "authorisation is server-side in config/service.yaml",
                   **body}, fh, indent=2)
    print(f"  [10-service-layer-refusal] as teller1: {body['status']}  "
          f"{body['refusal']['code']}  http={resp.status_code}  "
          f"steps_executed=0 (no browser opened)")


def main() -> int:
    os.makedirs(ROOT, exist_ok=True)
    target = TargetControl().signon()
    target.apply("0.0", "")
    print("[target] baseline: no injected faults")

    ledger = Ledger(os.path.join(ROOT, "value_ledger.jsonl"))
    if os.path.exists(ledger.path):
        os.remove(ledger.path)

    # ---- reads and the outcomes that are answers, not errors ---------------
    run("01-balances", "meridian.member_lookup", "teller1", member_id=MEMBER)
    run("02-member-search", "meridian.member_search", "teller1", last_name="Turing")
    run("03-no-such-member", "meridian.member_lookup", "teller1", member_id="999999")

    # ---- money movement, and the guardrails around it ----------------------
    run("04-transfer-posted", "meridian.transfer_funds", "teller1",
        allow_risky=True, ledger=ledger, member_id=MEMBER,
        from_share=f"{MEMBER}-S0070", to_share=f"{MEMBER}-S0001-3",
        amount="1.00", memo="evidence scenario")
    # Deliberately UNDER the $1,000 dual-control threshold: at $4,000 our own
    # value gate refuses first and the run never reaches the host, which would
    # demonstrate the wrong layer. The point of this scenario is the
    # APPLICATION's answer, so the amount has to be one our policy permits.
    # Same source share scenario 04 just moved money out of, so it is known to be
    # OPEN: the target is stateful and shared, and picking a share this harness
    # has not just exercised is how a scenario ends up demonstrating
    # SOURCE_SHARE_ON_HOLD when it meant to demonstrate an overdraw.
    run("05-insufficient-funds", "meridian.transfer_funds", "teller1",
        allow_risky=True, ledger=ledger, member_id=MEMBER,
        from_share=f"{MEMBER}-S0070", to_share=f"{MEMBER}-S0001",
        amount="900.00", memo="evidence scenario")
    run("06-over-ceiling-refused", "meridian.transfer_funds", "teller1",
        allow_risky=True, ledger=ledger, member_id=MEMBER,
        from_share=f"{MEMBER}-S0070", to_share=f"{MEMBER}-S0001-3",
        amount="9000.00", memo="evidence scenario")

    # ---- a substring search is three different questions -------------------
    # Meridian matches member numbers on SUBSTRING, which gives three ways to be
    # uncertain about WHO was asked for. Every one of them is a question rather
    # than a fault, and answering any of them with somebody else's balances is
    # the worst outcome this system can produce.
    run("13-ambiguous-member", "meridian.member_lookup", "teller1",
        member_id="100")
    run("14-member-number-not-exact", "meridian.member_lookup", "teller1",
        member_id="1002")

    # ---- injected runtime conditions ---------------------------------------
    # A TRANSIENT window, not a permanent one. Holding the interstitial down for
    # the whole run would prove only that a bounded retry gives up, which is
    # scenario 08's job; clearing it mid-run is what actually exercises
    # detect -> recover -> re-scan -> carry on, and it is what a real
    # maintenance blip looks like.
    target.apply("1.0", "maintenance")
    print("[target] forcing: maintenance interstitial, clearing after 4s")
    clearer = threading.Timer(4.0, lambda: TargetControl().signon().apply("0.0", ""))
    clearer.start()
    run("07-maintenance-recovered", "meridian.member_lookup", "teller1",
        member_id=MEMBER)
    clearer.cancel()
    target.apply("1.0", "timeout")
    print("[target] forcing: session expiry on every request")
    run("08-session-expired", "meridian.member_lookup", "teller1", member_id=MEMBER)
    target.apply("0.0", "")
    print("[target] baseline restored")

    # ---- authorisation, at both layers -------------------------------------
    # ---- the SERVICE layer refusing before anything is driven --------------
    _service_layer_refusal()

    run("09-supervisor-required", "meridian.place_hold", "teller1",
        allow_risky=True, member_id=MEMBER, share_id=f"{MEMBER}-S0070",
        reason_code="FRAUD", notes="evidence scenario")
    run("11-escalation-unattended", "meridian.place_hold", "super1",
        member_id=MEMBER, share_id=f"{MEMBER}-S0070", reason_code="FRAUD",
        notes="evidence scenario",
        coordinator_timeout=8, cdp_port=9224)
    escalation_resolved()

    # ---- the rest of the function surface, replayed ------------------------
    # Discovery evidence existed for all seven capabilities; replay evidence did
    # not. Replay is the production path, so a capability with only a discovery
    # run behind it is a capability nobody has watched work the way it will
    # actually run.
    target.apply("0.0", "")
    run("15-signon", "meridian.signon", "teller1")
    run("16-open-share-posted", "meridian.open_share", "teller1",
        allow_risky=True, ledger=ledger, member_id=MEMBER,
        share_type="SHARE", initial_deposit="25.00")
    run("17-contact-details-updated", "meridian.update_member_info", "teller1",
        allow_risky=True, member_id=MEMBER,
        email="evidence@example.com", phone="555-0142",
        address="1 Evidence Way")

    # ---- the inject kinds the brief names, one run each ---------------------
    # A TRANSIENT window, like scenario 07: what is being demonstrated is
    # detect -> recover -> re-scan -> carry on. APPLICATION_ERROR is declared
    # recoverable with a bounded reload, and is additionally refused outright on
    # irreversible steps -- reloading a posted transfer is how you post it twice.
    target.apply("1.0", "server")
    print("[target] forcing: HTTP 500 application error, clearing after 4s")
    clearer = threading.Timer(4.0, lambda: TargetControl().signon().apply("0.0", ""))
    clearer.start()
    run("18-application-error-recovered", "meridian.member_lookup", "teller1",
        member_id=MEMBER)
    clearer.cancel()

    # The other half of the recoverable contract, which nothing else covered: a
    # recoverable condition that does NOT recover. Held down for the whole run,
    # so the bounded retry budget is spent and the run stops rather than looping
    # -- a retry policy with no exhaustion path is an infinite loop with good
    # manners.
    target.apply("1.0", "validation")
    print("[target] forcing: validation rejection on every request")
    run("19-validation-persistent", "meridian.member_lookup", "teller1",
        member_id=MEMBER)

    # Both of these have a NATURAL counterpart already in the set (03 is a
    # search that matched nothing; 09 is the host's own supervisor refusal). The
    # injected pages are a different rendering of the same conditions, so these
    # two runs are what proves the detectors match the injected page as well as
    # the organic one. If either comes back `failure`, that is the finding: the
    # condition library needs a detector for the injected variant.
    target.apply("1.0", "notfound")
    print("[target] forcing: 404 member record not found")
    run("20-notfound-injected", "meridian.member_lookup", "teller1",
        member_id=MEMBER)

    target.apply("1.0", "permission")
    print("[target] forcing: 403 supervisor override required")
    run("21-permission-injected", "meridian.member_lookup", "teller1",
        member_id=MEMBER)

    target.apply("0.0", "")
    print("[target] baseline restored")
    print(f"[evidence] scenarios written under {ROOT}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
