#!/usr/bin/env python3
"""
Reproducible evidence generator. Self-contained: it starts the mock tenants it
needs as subprocesses, runs one scenario per evidence directory, and tears them
down. Assumes capabilities/*.json already exist (see scripts/run_discovery.sh).

  Tenants:
    corebank / demo-cu   -> 127.0.0.1:5057  (base tenant the artifact was recorded on)
    summit   / summit-cu -> 127.0.0.1:5059  (same vendor product, rebranded labels)

  Scenarios:
    01 success            06 escalation + live handoff (CDP)
    02 not-found          07 cross-tenant: Summit WITH override  (clean, no drift)
    03 permission-denied  08 cross-tenant: Summit NO map         (works via fallback, drift)
    04 interstitial       09 stability: N replays -> pass rate
    05 session-timeout    10 fill silently discarded (verify_value)
                          11 value ceiling refused
                          12 dual control unmet (fails closed)
                          13 dual control counter-signed
                          14 second surface: a11y tree, no DOM
                          15 velocity ceiling across runs (value ledger)

Usage: python scripts/gen_evidence.py
"""
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))
os.chdir(os.path.abspath(os.path.dirname(__file__) + "/.."))

from bankcua.schema import CapabilityArtifact
from bankcua.replay.engine import ReplayEngine
from bankcua.observability.logging import RunLogger
from bankcua.safety.policy import Policy, PolicyEngine
from bankcua.surface.accessibility import AccessibilitySurface
from bankcua.surface.web_playwright import WebSurface
from bankcua.escalation.handoff import HandoffStore, HandoffCoordinator, OperatorSession
from bankcua.tenancy import TenantOverride, apply_overrides

POL = Policy.from_yaml("config/policy.yaml")
def _artifact(path):
    with open(path) as f:
        return CapabilityArtifact.from_json(f.read())


LOOKUP = _artifact("capabilities/corebank.member_savings_lookup.json")
SUB = _artifact("capabilities/corebank.open_subaccount.json")
CREDS = {"username": "operator", "password": "password123"}
_PROCS = []


def _up(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_tenant(port, variant):
    if _up(port):
        return
    env = dict(os.environ, MOCKBANK_PORT=str(port), MOCKBANK_VARIANT=variant)
    p = subprocess.Popen([sys.executable, "mockbank/app.py"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _PROCS.append(p)
    for _ in range(40):
        if _up(port):
            return
        time.sleep(0.25)
    raise RuntimeError(f"tenant on :{port} did not start")


def stop_tenants():
    for p in _PROCS:
        p.terminate()


def control(base, k, v=""):
    urllib.request.urlopen(f"{base}/_control/set?key={k}&value={v}").read()


def reset(base):
    urllib.request.urlopen(f"{base}/_control/reset").read()


def run_replay(run_dir, art, params, allow_risky=False, policy=None,
               surface_cls=WebSurface, **engine_kw):
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    logger = RunLogger(run_dir, "replay", art.secret_params(),
                       {n: str(params[n]) for n in art.secret_params()
                        if params.get(n)})
    pe = PolicyEngine(policy or POL,
                      artifact_url_patterns=art.target.allowed_url_patterns,
                      allow_risky_override=allow_risky)
    surf = surface_cls(art.target.base_url, headless=True)
    surf.start()
    try:
        res = ReplayEngine(surf, pe, logger, None, **engine_kw).run(art, params)
    finally:
        surf.stop()
    logger.finish(res.model_dump())
    return res


def line(tag, res):
    s = f"[{tag}] status={res.status.value}"
    if res.business_outcome:
        s += f" outcome={res.business_outcome.code}"
        if res.business_outcome.outputs_surfaced:
            s += f" surfaced={res.business_outcome.outputs_surfaced}"
    if res.refusal:
        where = ("@step%d" % res.refusal.step_index
                 if res.refusal.step_index is not None else " (pre-flight)")
        s += f" refused={res.refusal.code}{where}"
    if res.failure:
        # a pre-flight refusal has no step: it happens before anything is opened
        where = ("@step%d" % res.failure.step_index
                 if res.failure.step_index is not None else " (pre-flight)")
        s += f" failure={res.failure.code}{where}"
    if res.outputs:
        s += f" outputs={res.outputs}"
    if res.recoveries:
        s += f" recovered={[r.condition_code for r in res.recoveries]}"
    if res.drifts:
        s += f" drift_steps={[d.step_index for d in res.drifts]}"
    print(s)


def main():
    start_tenant(5057, "corebank")
    start_tenant(5059, "summit")
    base = "http://127.0.0.1:5057"
    try:
        reset(base)
        line("01-success", run_replay("evidence/replay-01-success", LOOKUP,
             {**CREDS, "member_id": "12345"}))
        line("02-not-found", run_replay("evidence/replay-02-not-found", LOOKUP,
             {**CREDS, "member_id": "00000"}))
        line("03-permission-denied", run_replay("evidence/replay-03-permission-denied",
             LOOKUP, {**CREDS, "member_id": "99999"}))

        control(base, "inject", "interstitial")
        line("04-interstitial-recovered",
             run_replay("evidence/replay-04-interstitial-recovered", LOOKUP,
                        {**CREDS, "member_id": "12345"}))
        reset(base)

        control(base, "timeout", "on")
        line("05-session-timeout", run_replay("evidence/replay-05-session-timeout",
             LOOKUP, {**CREDS, "member_id": "12345"}))
        reset(base)

        escalation_handoff()
        cross_tenant()
        stability()
        fill_not_applied(base)
        value_policy()
        second_surface()
        velocity_limit()
    finally:
        stop_tenants()


def escalation_handoff():
    run_dir = "evidence/escalation-06-handoff"
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    store = HandoffStore("evidence/handoffs")
    logger = RunLogger(run_dir, "replay", SUB.secret_params(), dict(CREDS))
    pe = PolicyEngine(POL, artifact_url_patterns=SUB.target.allowed_url_patterns,
                      allow_risky_override=False)
    surf = WebSurface(SUB.target.base_url, headless=True, cdp_port=9222)
    surf.start()
    coord = HandoffCoordinator(store, logger)
    req_id = f"replay-{SUB.id}-step9"

    def operator():
        for _ in range(120):
            try:
                r = store.read(req_id)
                if r.status.value == "open" and r.cdp_endpoint:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            return
        sess = OperatorSession(store.read(req_id), store).attach()
        try:
            sess.do("note", "reviewed irreversible sub-account creation; approving")
            sess.do("click_selector", "input[type=submit]")
            time.sleep(0.4)
            sess.resolve(note="performed manually by operator", resume=True)
        finally:
            sess.detach()

    t = threading.Thread(target=operator)
    t.start()
    try:
        res = ReplayEngine(surf, pe, logger, coord).run(
            SUB, {**CREDS, "member_id": "12345", "acct_type": "Money Market",
                  "deposit": "500.00"})
    finally:
        t.join(timeout=5)
        surf.stop()
    logger.finish(res.model_dump())
    fin = store.read(req_id)
    print(f"[06-escalation-handoff] status={res.status.value} outputs={res.outputs} "
          f"human_actions={len(fin.human_actions)} final_controller={fin.controller}")


def cross_tenant():
    # 07: same artifact + tenant override -> clean success on Summit (no drift)
    ov = TenantOverride.load("config/tenants/summit-cu.json")
    art = apply_overrides(LOOKUP, ov)
    line("07-crosstenant-summit-override",
         run_replay("evidence/replay-07-crosstenant-summit-override", art,
                    {**CREDS, "member_id": "12345"}))

    # 08: same artifact, NO string map -> still succeeds via structural fallback,
    # emitting drift signals (graceful degradation, not a cliff)
    nomap = TenantOverride(tenant_id="summit-cu", base_url="http://127.0.0.1:5059")
    art2 = apply_overrides(LOOKUP, nomap)
    line("08-crosstenant-summit-nomap",
         run_replay("evidence/replay-08-crosstenant-summit-nomap", art2,
                    {**CREDS, "member_id": "12345"}))


def fill_not_applied(base):
    """10: a fill the page silently discards.

    The injected input ACCEPTS the keystrokes and throws them away, so the action
    reports success and the page looks entirely normal. No page-state checkpoint
    can see this -- only reading the control back does."""
    control(base, "inject", "swallow")
    try:
        line("10-fill-not-applied",
             run_replay("evidence/replay-10-fill-not-applied", LOOKUP,
                        {**CREDS, "member_id": "12345"}))
    finally:
        reset(base)


def value_policy():
    """11-13: the semantic layer. The URL/action allowlist cannot tell $1 from
    $1M; these rules read the invocation's inputs before the browser opens."""
    from bankcua.safety.policy import ValueRule
    pol = Policy.from_yaml("config/policy.yaml")
    pol.value_rules = {"deposit": ValueRule(max=10_000.0, dual_control_above=1_000.0,
                                            unit=" USD")}
    base_params = {**CREDS, "member_id": "12345", "acct_type": "Money Market"}


    # 11: hard ceiling -> refused outright, nothing opened
    line("11-value-limit-exceeded",
         run_replay("evidence/replay-11-value-limit-exceeded", SUB,
                    {**base_params, "deposit": "25000.00"},
                    allow_risky=True, policy=pol))

    # 12: over the dual-control threshold, nobody counter-signs -> fails closed
    line("12-dual-control-unmet",
         run_replay("evidence/replay-12-dual-control-unmet", SUB,
                    {**base_params, "deposit": "1500.00"},
                    allow_risky=True, policy=pol))

    # 13: independent second approver -> clears the VALUE gate, then is stopped by
    # the independent STEP gate on the irreversible click. Two layers, both live.
    line("13-dual-control-countersigned",
         run_replay("evidence/replay-13-dual-control-countersigned", SUB,
                    {**base_params, "deposit": "1500.00"},
                    allow_risky=True, policy=pol,
                    initiator="alice", approver="bruce"))


def second_surface():
    """14: the SAME artifact, replayed through a surface with no DOM.

    Perception is an accessibility tree; action is a mouse click and keystrokes.
    Nothing in the schema, compiler, replay engine, error taxonomy or safety model
    differs between this run and scenario 01 -- only the Surface implementation.
    A static portability report is written alongside, because whether an artifact
    CAN run on a surface is decidable before anything is launched."""
    from bankcua.portability import portability_report
    run_dir = "evidence/replay-14-second-surface-a11y"
    res = run_replay(run_dir, LOOKUP, {**CREDS, "member_id": "12345"},
                     surface_cls=AccessibilitySurface)
    line("14-second-surface-a11y", res)
    rep = portability_report(LOOKUP, AccessibilitySurface, "a11y")
    with open(os.path.join(run_dir, "portability.json"), "w") as f:
        f.write(rep.model_dump_json(indent=2))
    print(f"    portability: {rep.summary()}")


def velocity_limit():
    """15: the limit a per-invocation ceiling cannot see.

    Every amount here is individually legal against `max`; only the trailing SUM
    is not. Ten $999 deposits clear a $1,000 limit ten times over, which is the
    shape real money-movement abuse takes, so the refusal has to come from
    history rather than from the request alone. The prior spend is seeded under a
    DIFFERENT capability, because the budget belongs to the parameter, not to one
    flow -- splitting it per flow is the gap an attacker walks through.
    """
    import time as _time

    from bankcua.safety.ledger import Ledger, LedgerEntry
    from bankcua.safety.policy import ValueRule

    vpol = Policy.from_yaml("config/policy.yaml")
    vpol.value_rules = {"deposit": ValueRule(max=10_000.0, max_per_window=5_000.0,
                                             window_seconds=3600, unit=" USD")}
    # The ledger is cross-run state by definition, so it sits beside the run
    # directory rather than inside one -- a velocity budget scoped to a single run
    # is not a velocity budget. Deliberately NOT the CLI's default
    # (evidence/value_ledger.jsonl): this scenario seeds a $4,500 spend to make
    # the window bite, and leaving that in the path a reader's own `replay` picks
    # up would refuse their next legitimate deposit for reasons invisible to them.
    # Cleared first so the scenario is deterministic.
    ledger_path = "evidence/replay-15-velocity-limit.ledger.jsonl"
    if os.path.exists(ledger_path):
        os.remove(ledger_path)
    ledger = Ledger(ledger_path)
    ledger.record(LedgerEntry(ts=_time.time(),
                              capability_id="corebank.some_other_flow",
                              param="deposit", value=4500.0))
    line("15-velocity-limit",
         run_replay("evidence/replay-15-velocity-limit", SUB,
                    {**CREDS, "member_id": "12345", "acct_type": "Money Market",
                     "deposit": "900.00"},
                    allow_risky=True, policy=vpol, ledger=ledger))


def stability():
    # 09: replay N times -> pass rate (flakiness signal)
    n, ok = 5, 0
    for i in range(n):
        r = run_replay(f"evidence/replay-09-stability/run{i}", LOOKUP,
                       {**CREDS, "member_id": "12345"})
        ok += 1 if r.status.value == "success" else 0
    print(f"[09-stability] {ok}/{n} passed  pass_rate={ok/n:.2f}")


if __name__ == "__main__":
    main()
