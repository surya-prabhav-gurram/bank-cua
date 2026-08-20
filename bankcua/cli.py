"""
Command-line entrypoints:

  discover  -- run an LLM-driven discovery run, save a CapabilityArtifact
  replay    -- deterministically replay an artifact with input params (no LLM)
  catalog   -- list / show saved capabilities, or print the agent manifest
  operator  -- stand-in operator console: take control of a live session to
               resolve an open intervention, then hand back

Run `python -m bankcua.cli <command> -h` for options.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

from .agent.compiler import compile_artifact
from .agent.loop import DiscoveryLoop
from .agent.providers import make_provider
from .agent.task import DiscoveryTask
from .catalog import Catalog
from .escalation.handoff import HandoffCoordinator, HandoffStore, OperatorSession
from .observability.logging import RunLogger
from .replay.engine import ReplayEngine
from .safety.policy import Policy, PolicyEngine
from .surface.accessibility import AccessibilitySurface
from .surface.web_playwright import WebSurface


def _ts() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def _load_policy(path: str) -> Policy:
    return Policy.from_yaml(path) if os.path.exists(path) else Policy(
        allowed_url_patterns=["http://127.0.0.1:*", "http://localhost:*"])


def _parse_params(args) -> dict:
    params = {}
    if args.params:
        params.update(json.loads(args.params))
    for kv in (args.param or []):
        k, _, v = kv.partition("=")
        params[k] = v
    return params


# ---------------------------------------------------------------------------
SURFACES = {"web": WebSurface, "a11y": AccessibilitySurface}


def _make_surface(args, base_url):
    """Build the requested Surface. The engines never see this choice -- they
    only ever hold the abstract interface, which is the whole point of §4."""
    kind = getattr(args, "surface", "web") or "web"
    if kind == "a11y":
        return AccessibilitySurface(base_url, headless=not args.headed)
    return WebSurface(base_url, headless=not args.headed,
                      cdp_port=getattr(args, "cdp_port", 0))


def cmd_discover(args):
    task = DiscoveryTask.load(args.task)
    secret_names = {p.name for p in task.inputs if p.sensitive}
    secret_values = {n: str(task.param_values[n]) for n in secret_names
                     if task.param_values.get(n)}

    run_id = f"discovery-{task.capability_id}-{_ts()}"
    run_dir = os.path.join(args.evidence, run_id)
    logger = RunLogger(run_dir, "discovery", secret_names, secret_values)

    policy = _load_policy(args.policy)
    engine_policy = PolicyEngine(policy, artifact_url_patterns=None,
                                 allow_risky_override=args.allow_risky)
    surface = WebSurface(task.base_url, headless=not args.headed,
                         cdp_port=args.cdp_port)
    surface.start()
    coordinator = HandoffCoordinator(HandoffStore(args.handoffs), logger)
    provider = make_provider(args.provider, bridge_dir=args.bridge_dir,
                             model=args.model, timeout_s=args.bridge_timeout)

    print(f"[discover] goal: {task.rendered_goal()}")
    print(f"[discover] provider={provider.name} evidence={run_dir}")
    try:
        loop = DiscoveryLoop(surface, provider, engine_policy, logger, coordinator)
        result = loop.run(task)
        print(f"[discover] status={result.status} reason={result.reason}")
        summary = {"status": result.status, "reason": result.reason,
                   "outputs": result.outputs,
                   "intervention_id": result.intervention_id,
                   "num_steps": len(result.transcript)}
        artifact_path = None
        if result.status == "success":
            art = compile_artifact(task, result, evidence_dir=run_dir,
                                   recorded_by=(provider.name if provider.name != "bridge"
                                                else "llm-bridge"),
                                   discovery_run_id=run_id)
            cat = Catalog(args.out)
            artifact_path = cat.save(art)
            summary["artifact_path"] = artifact_path
            print(f"[discover] artifact saved -> {artifact_path}")
        logger.finish(summary)
    finally:
        surface.stop()
    if not artifact_path and result.status != "success":
        sys.exit(2)


def _build_assist_provider(args):
    if not getattr(args, "assist", False):
        return None
    kind = args.assist_provider
    if kind == "bridge":
        return make_provider("bridge", bridge_dir=args.assist_bridge_dir,
                             timeout_s=args.bridge_timeout if hasattr(args, "bridge_timeout")
                             else 1800.0)
    return make_provider("anthropic", model=getattr(args, "model", None))


def cmd_replay(args):
    art = _load_artifact(args.artifact)
    if getattr(args, "tenant", None):
        from .tenancy import TenantOverride, apply_overrides
        ov = TenantOverride.load(args.tenant)
        art = apply_overrides(art, ov)
        print(f"[replay] tenant override: {ov.tenant_id} -> {art.target.base_url}")

    # approval gate: unattended replay of an unapproved capability is refused
    if getattr(args, "require_approved", False) and art.approval_state.value != "approved":
        print(f"[replay] REFUSED: capability '{art.id}' is {art.approval_state.value}, "
              f"not approved (run with human oversight or approve it first).")
        sys.exit(4)

    params = _parse_params(args)
    policy = _load_policy(args.policy)
    assist = _build_assist_provider(args)

    # A capability that can pause for a human is useless to that human unless the
    # session is exposed. Say so at the start, not when they are already staring
    # at a paused screen wondering why the console will not open.
    if not getattr(args, "cdp_port", 0) and any(
            st.requires_confirmation for st in art.steps):
        gated = [st.index for st in art.steps if st.requires_confirmation]
        print(f"[replay] NOTE: step(s) {gated} are gated for human confirmation, "
              f"but no --cdp-port was given. If this run pauses, an operator will "
              f"NOT be able to take control of the live session. Re-run with "
              f"--cdp-port 9222 to enable the operator console.")

    def one_run(tag=""):
        run_id = f"replay-{art.id}-{_ts()}{tag}"
        run_dir = os.path.join(args.evidence, run_id)
        logger = RunLogger(run_dir, "replay", art.secret_params(),
                           {n: str(params[n]) for n in art.secret_params()
                            if params.get(n)})
        engine_policy = PolicyEngine(
            policy, artifact_url_patterns=art.target.allowed_url_patterns,
            allow_risky_override=args.allow_risky)
        surface = _make_surface(args, art.target.base_url)
        surface.start()
        coordinator = HandoffCoordinator(
            HandoffStore(args.handoffs), logger,
            wait_timeout_s=getattr(args, "handoff_timeout", 120.0))
        res = None
        try:
            engine = ReplayEngine(surface, engine_policy, logger, coordinator,
                                  assist_provider=assist,
                                  max_assists=args.max_assists,
                                  escalate_unrecoverable=getattr(
                                      args, "escalate_unrecoverable", False),
                                  initiator=getattr(args, "initiator", ""),
                                  approver=getattr(args, "approver", ""))
            res = engine.run(art, params)
            # Drift is only meaningful as a trend, so every run contributes to the
            # history the repair loop reasons over.
            try:
                from .repair import DriftLedger
                DriftLedger(os.path.join(args.evidence, "drift_ledger.jsonl")) \
                    .record_result(res, tenant_id=art.target.tenant_id)
            except Exception:
                pass
        finally:
            logger.finish(json.loads(res.model_dump_json()) if res else {})
            surface.stop()
        return res

    repeat = max(1, args.repeat)
    if repeat == 1:
        result = one_run()
        print(f"[replay] capability={art.id} v{art.version}")
        print("[replay] RESULT:")
        print(result.model_dump_json(indent=2))
        if result.status.value == "failure":
            sys.exit(3)
        return

    # stability mode: N runs -> pass rate
    passes, statuses = 0, []
    for i in range(repeat):
        r = one_run(tag=f"-run{i}")
        statuses.append(r.status.value)
        passes += 1 if r.status.value == "success" else 0
    rate = passes / repeat
    print(f"[replay] STABILITY: {passes}/{repeat} success  pass_rate={rate:.2f}")
    print(f"[replay] statuses: {statuses}")
    if args.update_stability:
        from .schema import StabilitySignal
        art.stability = StabilitySignal(runs=repeat, passes=passes)
        _save_artifact(args.artifact, art)
        print(f"[replay] wrote stability to {args.artifact}")


def _save_artifact(path, art):
    with open(path, "w") as f:
        f.write(art.to_json())


def _load_artifact(path: str):
    from .schema import CapabilityArtifact
    return CapabilityArtifact.from_json(open(path).read())


def cmd_catalog(args):
    cat = Catalog(args.dir)
    if args.action == "list":
        for a in cat.list():
            print(f"- {a.id} v{a.version} [{a.approval_state.value}] : {a.name}")
            print(f"    inputs:  {[p.name for p in a.inputs]}")
            print(f"    outputs: {[o.name for o in a.outputs]}")
    elif args.action == "show":
        print(cat.get(args.id).to_json())
    elif args.action == "manifest":
        print(json.dumps(cat.manifest(), indent=2))
    elif args.action == "portability":
        from .portability import portability_report
        art = cat.get(args.id)
        for name in sorted(SURFACES):
            print(portability_report(art, SURFACES[name], name).summary())
    elif args.action == "review":
        art = cat.review_step(args.id, args.step, risk=args.risk, note=args.note)
        st = next(x for x in art.steps if x.index == args.step)
        print(f"[catalog] '{args.id}' step {args.step} reviewed -> "
              f"{st.risk.value} ({st.risk_reason})")
        pending = cat.unreviewed_risky_steps(art)
        print(f"[catalog] risky steps still unreviewed: {pending or 'none'}")
    elif args.action == "approve":
        try:
            cat.approve(args.id)
        except ValueError as ex:
            print(f"[catalog] REFUSED: {ex}")
            sys.exit(5)
        print(f"[catalog] '{args.id}' -> approved")


def cmd_operator(args):
    store = HandoffStore(args.handoffs)
    if args.action == "list":
        for r in store.list_open():
            attach = (f"console: python -m bankcua.cli operator console --id {r.id}"
                      if r.cdp_endpoint else
                      "NOT ATTACHABLE (run started without --cdp-port)")
            print(f"- {r.id} [{r.kind.value}] step={r.current_step_index} "
                  f"reason={r.reason}")
            print(f"    url={r.state_url} cdp={r.cdp_endpoint or '-'}")
            print(f"    {attach}")
            print(f"    screenshot={r.screenshot_path}")
        return
    if args.action == "console":
        from .escalation.console import NotAttachable, create_console
        try:
            app = create_console(args.id, handoffs=args.handoffs)
        except NotAttachable as ex:
            print(f"[operator] CANNOT ATTACH: {ex}")
            sys.exit(7)
        print(f"[operator] console for {args.id} on "
              f"http://{args.host}:{args.port} -- you are driving the SAME live "
              f"session the automation paused")
        app.run(host=args.host, port=args.port, threaded=True)
        return
    if args.action == "resolve":
        req = store.read(args.id)
        print(f"[operator] taking control of live session at {req.cdp_endpoint}")
        sess = OperatorSession(req, store).attach()
        try:
            for spec in (args.do or []):
                op, _, rest = spec.partition(":")
                detail, _, value = rest.partition("=")
                print(f"[operator] {op} {detail} {value}")
                sess.do(op, detail, value or None)
            note = args.note or ("performed manually" if args.manual else "approved")
            sess.resolve(note=note, resume=not args.no_resume)
            print(f"[operator] resolved (resume={not args.no_resume}); control -> agent")
        finally:
            sess.detach()


def cmd_repair(args):
    from .repair import DriftLedger, ProposalStore, analyse, apply as apply_repair
    cat = Catalog(args.dir)
    ledger = DriftLedger(os.path.join(args.evidence, "drift_ledger.jsonl"))
    store = ProposalStore(os.path.join(args.evidence, "repairs"))

    if args.action == "analyse":
        art = cat.get(args.id)
        proposal = analyse(art, ledger, min_occurrences=args.min_occurrences,
                           tenant_id=args.tenant_id)
        print(proposal.summary())
        if proposal.repairs or proposal.unrepairable:
            print(f"[repair] wrote {store.save(proposal)}")
        return

    if args.action == "list":
        for p in store.list():
            print(f"- {p.id} [{'applied' if p.applied else 'open'}] "
                  f"{len(p.repairs)} repair(s), {len(p.unrepairable)} needing a human")
        return

    if args.action == "apply":
        proposal = store.load(args.id)
        art = cat.get(proposal.capability_id)
        if art.version != proposal.from_version:
            print(f"[repair] REFUSED: proposal targets {proposal.from_version}, "
                  f"catalog holds {art.version}")
            sys.exit(6)
        repaired = apply_repair(art, proposal)
        cat.save(repaired)
        proposal.applied = True
        store.save(proposal)
        print(f"[repair] {repaired.id} -> v{repaired.version} "
              f"[{repaired.approval_state.value}]")
        print("[repair] a human must approve it before unattended replay "
              "(`catalog approve`)")
        return


def cmd_serve(args):
    from .service import create_app
    app = create_app(catalog_dir=args.dir, policy_path=args.policy,
                     evidence_dir=args.evidence)
    print(f"[serve] capability API on http://{args.host}:{args.port}")
    print("  GET  /capabilities            list (agent manifest)")
    print("  GET  /capabilities/<id>       full contract")
    print("  POST /invoke/<id>             {params, tenant?, allow_risky?}")
    app.run(host=args.host, port=args.port, threaded=True)


def cmd_codegen(args):
    from .codegen import generate_playwright_script
    art = _load_artifact(args.artifact)
    code = generate_playwright_script(art)
    if args.out:
        with open(args.out, "w") as f:
            f.write(code)
        print(f"[codegen] wrote {args.out}")
    else:
        print(code)


def build_parser():
    p = argparse.ArgumentParser(prog="bankcua")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("discover", help="LLM-driven discovery -> artifact")
    d.add_argument("--task", required=True)
    d.add_argument("--provider", default="bridge", choices=["bridge", "anthropic"])
    d.add_argument("--bridge-dir", default="evidence/bridge")
    d.add_argument("--bridge-timeout", type=float, default=1800.0,
                   help="seconds the bridge waits for a decision before exiting")
    d.add_argument("--model", default=None,
                   help="LLM model id for --provider anthropic (or set LLM_MODEL)")
    d.add_argument("--out", default="capabilities")
    d.add_argument("--evidence", default="evidence")
    d.add_argument("--policy", default="config/policy.yaml")
    d.add_argument("--handoffs", default="evidence/handoffs")
    d.add_argument("--headed", action="store_true")
    d.add_argument("--cdp-port", type=int, default=0)
    d.add_argument("--allow-risky", action="store_true")
    d.set_defaults(func=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay (no LLM)")
    r.add_argument("--artifact", required=True)
    r.add_argument("--tenant", help="tenant override JSON (cross-tenant reuse)")
    r.add_argument("--params", help="JSON object of inputs")
    r.add_argument("--param", action="append", help="k=v (repeatable)")
    r.add_argument("--evidence", default="evidence")
    r.add_argument("--policy", default="config/policy.yaml")
    r.add_argument("--handoffs", default="evidence/handoffs")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--cdp-port", type=int, default=0)
    r.add_argument("--allow-risky", action="store_true")
    r.add_argument("--handoff-timeout", type=float, default=120.0,
                   help="seconds automation holds the paused session open for a "
                        "human operator before aborting (default 120)")
    r.add_argument("--surface", choices=sorted(SURFACES), default="web",
                   help="which Surface implementation to replay through; the "
                        "artifact is unchanged either way")
    r.add_argument("--initiator", default="",
                   help="who is running this capability (dual control)")
    r.add_argument("--approver", default="",
                   help="independent second approver for value-policy dual "
                        "control; must differ from --initiator")
    r.add_argument("--escalate-unrecoverable", action="store_true",
                   help="route an unrecoverable replay failure to a human "
                        "operator instead of failing outright")
    r.add_argument("--repeat", type=int, default=1,
                   help="run N times and report a stability/flakiness pass rate")
    r.add_argument("--update-stability", action="store_true",
                   help="with --repeat, write the pass rate back into the artifact")
    r.add_argument("--require-approved", action="store_true",
                   help="refuse to replay unless approval_state == approved")
    r.add_argument("--assist", action="store_true",
                   help="enable bounded, policy-checked single-step LLM recovery")
    r.add_argument("--assist-provider", default="anthropic",
                   choices=["anthropic", "bridge"])
    r.add_argument("--assist-bridge-dir", default="evidence/assist_bridge")
    r.add_argument("--max-assists", type=int, default=1)
    r.add_argument("--model", default=None)
    r.add_argument("--bridge-timeout", type=float, default=1800.0)
    r.set_defaults(func=cmd_replay)

    c = sub.add_parser("catalog", help="list / show / manifest / approve")
    c.add_argument("action",
                   choices=["list", "show", "manifest", "portability",
                            "review", "approve"])
    c.add_argument("--id")
    c.add_argument("--step", type=int,
                   help="step index to review (catalog review)")
    c.add_argument("--risk", choices=["safe", "risky"],
                   help="reclassify the step while reviewing it")
    c.add_argument("--note", default="",
                   help="reviewer's justification, recorded on the step")
    c.add_argument("--dir", default="capabilities")
    c.set_defaults(func=cmd_catalog)

    o = sub.add_parser("operator", help="operator console (handoff)")
    o.add_argument("action", choices=["list", "console", "resolve"])
    o.add_argument("--host", default="127.0.0.1")
    o.add_argument("--port", type=int, default=8090)
    o.add_argument("--id")
    o.add_argument("--do", action="append",
                   help="operator op, e.g. 'click_text:Continue to application'")
    o.add_argument("--manual", action="store_true",
                   help="operator performed the gated step manually")
    o.add_argument("--note", default="")
    o.add_argument("--no-resume", action="store_true")
    o.add_argument("--handoffs", default="evidence/handoffs")
    o.set_defaults(func=cmd_operator)

    rp = sub.add_parser("repair", help="drift-driven artifact repair proposals")
    rp.add_argument("action", choices=["analyse", "list", "apply"])
    rp.add_argument("--id", help="capability id (analyse) or proposal id (apply)")
    rp.add_argument("--dir", default="capabilities")
    rp.add_argument("--evidence", default="evidence")
    rp.add_argument("--tenant-id", default=None)
    rp.add_argument("--min-occurrences", type=int, default=3,
                    help="drifts on the same step before a repair is proposed")
    rp.set_defaults(func=cmd_repair)

    s = sub.add_parser("serve", help="agent-facing capability API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--dir", default="capabilities")
    s.add_argument("--policy", default="config/policy.yaml")
    s.add_argument("--evidence", default="evidence/service")
    s.set_defaults(func=cmd_serve)

    g = sub.add_parser("codegen", help="emit a runnable Playwright script from an artifact")
    g.add_argument("--artifact", required=True)
    g.add_argument("--out", help="write to file (default: stdout)")
    g.set_defaults(func=cmd_codegen)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
