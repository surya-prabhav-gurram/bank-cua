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
    05 session-timeout

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
from bankcua.surface.web_playwright import WebSurface
from bankcua.escalation.handoff import HandoffStore, HandoffCoordinator, OperatorSession
from bankcua.tenancy import TenantOverride, apply_overrides

POL = Policy.from_yaml("config/policy.yaml")
LOOKUP = CapabilityArtifact.from_json(open("capabilities/corebank.member_savings_lookup.json").read())
SUB = CapabilityArtifact.from_json(open("capabilities/corebank.open_subaccount.json").read())
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


def run_replay(run_dir, art, params, allow_risky=False):
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    logger = RunLogger(run_dir, "replay", art.secret_params(),
                       [str(params.get(n)) for n in art.secret_params()])
    pe = PolicyEngine(POL, artifact_url_patterns=art.target.allowed_url_patterns,
                      allow_risky_override=allow_risky)
    surf = WebSurface(art.target.base_url, headless=True)
    surf.start()
    try:
        res = ReplayEngine(surf, pe, logger, None).run(art, params)
    finally:
        surf.stop()
    logger.finish(res.model_dump())
    return res


def line(tag, res):
    s = f"[{tag}] status={res.status.value}"
    if res.business_outcome:
        s += f" outcome={res.business_outcome.code}"
    if res.failure:
        s += f" failure={res.failure.code}@step{res.failure.step_index}"
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
    finally:
        stop_tenants()


def escalation_handoff():
    run_dir = "evidence/escalation-06-handoff"
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir)
    store = HandoffStore("evidence/handoffs")
    logger = RunLogger(run_dir, "replay", SUB.secret_params(), ["operator", "password123"])
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
