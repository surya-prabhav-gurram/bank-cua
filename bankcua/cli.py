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
def cmd_discover(args):
    task = DiscoveryTask.load(args.task)
    secret_names = {p.name for p in task.inputs if p.sensitive}
    secret_values = [str(task.param_values[n]) for n in secret_names
                     if task.param_values.get(n)]

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

    def one_run(tag=""):
        run_id = f"replay-{art.id}-{_ts()}{tag}"
        run_dir = os.path.join(args.evidence, run_id)
        logger = RunLogger(run_dir, "replay", art.secret_params(),
                           [str(params[n]) for n in art.secret_params() if params.get(n)])
        engine_policy = PolicyEngine(
            policy, artifact_url_patterns=art.target.allowed_url_patterns,
            allow_risky_override=args.allow_risky)
        surface = WebSurface(art.target.base_url, headless=not args.headed,
                             cdp_port=args.cdp_port)
        surface.start()
        coordinator = HandoffCoordinator(HandoffStore(args.handoffs), logger)
        res = None
        try:
            engine = ReplayEngine(surface, engine_policy, logger, coordinator,
                                  assist_provider=assist,
                                  max_assists=args.max_assists)
            res = engine.run(art, params)
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
    elif args.action == "approve":
        from .schema import ApprovalState
        art = cat.get(args.id)
        art.approval_state = ApprovalState.APPROVED
        cat.save(art)
        print(f"[catalog] '{args.id}' -> approved")


def cmd_operator(args):
    store = HandoffStore(args.handoffs)
    if args.action == "list":
        for r in store.list_open():
            print(f"- {r.id} [{r.kind.value}] step={r.current_step_index} "
                  f"reason={r.reason}")
            print(f"    url={r.state_url} cdp={r.cdp_endpoint}")
            print(f"    screenshot={r.screenshot_path}")
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
    c.add_argument("action", choices=["list", "show", "manifest", "approve"])
    c.add_argument("--id")
    c.add_argument("--dir", default="capabilities")
    c.set_defaults(func=cmd_catalog)

    o = sub.add_parser("operator", help="operator console (handoff)")
    o.add_argument("action", choices=["list", "resolve"])
    o.add_argument("--id")
    o.add_argument("--do", action="append",
                   help="operator op, e.g. 'click_text:Continue to application'")
    o.add_argument("--manual", action="store_true",
                   help="operator performed the gated step manually")
    o.add_argument("--note", default="")
    o.add_argument("--no-resume", action="store_true")
    o.add_argument("--handoffs", default="evidence/handoffs")
    o.set_defaults(func=cmd_operator)

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
