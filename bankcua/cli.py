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
    # `art` is about to become the RUN-BOUND artifact -- rebased onto a tenant,
    # with locator strings remapped. That object must never be written back to
    # the shared file it came from, so the on-disk one is kept separately.
    tenant_bound = None
    if getattr(args, "tenant", None):
        from .tenancy import TenantOverride, apply_overrides
        ov = TenantOverride.load(args.tenant)
        art = apply_overrides(art, ov)
        tenant_bound = ov.tenant_id
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

    # The velocity budget is memory, so it has to outlive the run that spends it.
    # One ledger for the whole invocation (including every --repeat run), living
    # beside the other cross-run state in evidence/, so a rolling-window ceiling
    # is enforced across runs rather than being config nobody reads.
    from .safety.ledger import Ledger
    ledger = Ledger(os.path.join(args.evidence, "value_ledger.jsonl"))

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
                                  approver=getattr(args, "approver", ""),
                                  ledger=ledger)
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
        if tenant_bound:
            # Two reasons, and either alone is enough. `art` is now the rebased
            # object -- writing it back would silently replace the shared
            # artifact's base_url, allowlist and remapped locator strings with
            # one tenant's. And even a clean write would be wrong: a pass rate
            # measured against Summit is not evidence about the capability, it is
            # evidence about the capability ON SUMMIT, and the artifact has one
            # field to hold it. Refuse rather than record a number under a label
            # that misstates what was measured.
            print(f"[replay] NOT writing stability: this run was bound to tenant "
                  f"'{tenant_bound}', so the pass rate describes that tenant, not "
                  f"the shared capability. Re-run without --tenant to record a "
                  f"stability signal for {args.artifact}.")
            return
        from .schema import StabilitySignal
        # reload from disk: `art` has been through a run and must not carry any
        # run-time mutation back into the catalog.
        on_disk = _load_artifact(args.artifact)
        on_disk.stability = StabilitySignal(runs=repeat, passes=passes)
        _save_artifact(args.artifact, on_disk)
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
        confirm = None
        if args.no_confirmation:
            confirm = False
        elif args.require_confirmation:
            confirm = True
        art = cat.review_step(args.id, args.step, risk=args.risk, note=args.note,
                              requires_confirmation=confirm)
        st = next(x for x in art.steps if x.index == args.step)
        print(f"[catalog] '{args.id}' step {args.step} reviewed -> "
              f"{st.risk.value} ({st.risk_reason})")
        pending = cat.unreviewed_risky_steps(art)
        print(f"[catalog] risky steps still unreviewed: {pending or 'none'}")
    elif args.action == "refresh-conditions":
        targets = [args.id] if args.id else [a.id for a in cat.list()]
        for cap_id in targets:
            art, diff = cat.refresh_conditions(cap_id)
            churn = ", ".join(f"{k}={v}" for k, v in diff.items() if v) or "no change"
            print(f"[catalog] {cap_id} -> v{art.version} "
                  f"[{art.approval_state.value}]  {churn}")
        print("[catalog] refreshed capabilities are DRAFT; a human approves them "
              "before unattended replay")
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
                     service_config_path=args.service_config,
                     evidence_dir=args.evidence, cdp_port=args.cdp_port,
                     handoff_timeout_s=args.handoff_timeout,
                     require_session=args.require_session or None)
    print(f"[serve] capability API on http://{args.host}:{args.port}")
    print("  GET  /capabilities            list (agent manifest)")
    print("  GET  /capabilities/<id>       full contract")
    print("  GET  /operators               aliases + roles (never secrets)")
    print("  POST /invoke/<id>             {params, operator}")
    print("  POST /session/signon          establish the signed-in operator's "
          "session")
    print("  GET  /runs, /runs/<id>        run history + evidence")
    if args.require_session:
        print("[serve] every invocation must carry a console session; the "
              "operator alias comes from the sign-in, not the request")
    print(f"[serve] authorisation from {args.service_config} "
          f"(risk, approval and roles are server-side, never request-supplied)")
    app.run(host=args.host, port=args.port, threaded=True)


def cmd_chat(args):
    from .chat.app import create_app
    app = create_app(api_url=args.api, router=_chat_router(args),
                     default_operator=args.operator)
    print(f"[chat] assistant on http://{args.host}:{args.port} -> API {args.api}")
    app.run(host=args.host, port=args.port, threaded=True)


def cmd_dashboard(args):
    from .dashboard import create_app
    app = create_app(catalog_dir=args.dir,
                     evidence_dirs=tuple(args.evidence), api_url=args.api)
    print(f"[dashboard] on http://{args.host}:{args.port}")
    print(f"[dashboard] reading evidence from {list(args.evidence)} (read-only)")
    app.run(host=args.host, port=args.port, threaded=True)


def cmd_portal(args):
    """The signed-in console: one origin, two tabs, one identity.

    Starts the sign-in page with the dashboard and the assistant mounted behind
    it. It does NOT start the capability API -- that is a separate process on
    purpose, because the console is a client of it like any other, and running
    them together would make it easy to believe the console has a private way in.
    """
    from .portal.app import create_app, init_files

    if args.action == "init":
        created = init_files(principals_path=args.principals,
                             session_key_path=args.session_key)
        print("[portal] " + ("created " + ", ".join(created) if created
                             else "nothing to do; both files already exist"))
        print(f"[portal] edit {args.principals} to change who may sign in; "
              f"`portal hash --password <password>` prints a hash to paste in")
        return
    if args.action == "hash":
        from .auth import hash_password
        if not args.password:
            print("[portal] usage: portal hash --password <password>")
            return
        print(hash_password(args.password))
        return

    init_files(principals_path=args.principals, session_key_path=args.session_key)
    app = create_app(api_url=args.api, catalog_dir=args.dir,
                     evidence_dirs=tuple(args.evidence),
                     handoff_dir=args.handoffs,
                     principals_path=args.principals,
                     session_key_path=args.session_key,
                     router=_chat_router(args))
    print(f"[portal] console on http://{args.host}:{args.port} -> API {args.api}")
    print(f"[portal] sign-ins from {args.principals}; roles and member scope are "
          f"enforced by the API, not by the page")
    print("[portal] staff pick an operator and are signed on to the target at "
          "sign-in; the sign-on capability is then withheld from the manifest "
          "and refused at /invoke for that session")
    print("[portal] start the API with --require-session so nothing reaches it "
          "without a signed-in person behind it")
    app.run(host=args.host, port=args.port, threaded=True)


def _chat_router(args):
    """The assistant's router, shared by `chat` and `portal`."""
    from .chat.router import LLMRouter, RuleRouter
    if getattr(args, "router", "rule") != "llm":
        return RuleRouter()
    try:
        router = LLMRouter(model=getattr(args, "model", None))
        print("[chat] routing with a live model over the published manifest")
        return router
    except Exception as ex:
        print(f"[chat] no model router ({ex}); falling back to rule routing")
        return RuleRouter()


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
                            "review", "approve", "refresh-conditions"])
    c.add_argument("--id")
    c.add_argument("--step", type=int,
                   help="step index to review (catalog review)")
    c.add_argument("--risk", choices=["safe", "risky"],
                   help="reclassify the step while reviewing it")
    c.add_argument("--note", default="",
                   help="reviewer's justification, recorded on the step")
    c.add_argument("--no-confirmation", action="store_true",
                   help="reviewed: this irreversible step may run in an "
                        "explicitly approved unattended run, because other "
                        "limits bound it. Recorded on the step.")
    c.add_argument("--require-confirmation", action="store_true",
                   help="reviewed: this step must always stop for a person")
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
    s.add_argument("--dir", default="capabilities/meridian")
    s.add_argument("--policy", default="config/policy.meridian.yaml")
    s.add_argument("--service-config", default="config/service.yaml",
                   help="server-side authorisation; never request-supplied")
    s.add_argument("--evidence", default="evidence/service")
    s.add_argument("--cdp-port", type=int, default=9222,
                   help="expose each run's browser here so an operator can take "
                        "control of a paused session")
    s.add_argument("--require-session", action="store_true",
                   help="refuse invocations that carry no console sign-in; the "
                        "role and member scope then come from the person, not "
                        "from the request body")
    s.add_argument("--handoff-timeout", type=float, default=90.0,
                   help="seconds a gated run holds the live session open for a "
                        "human before aborting")
    s.set_defaults(func=cmd_serve)

    ch = sub.add_parser("chat", help="conversational front door over the API")
    ch.add_argument("--host", default="127.0.0.1")
    ch.add_argument("--port", type=int, default=8081)
    ch.add_argument("--api", default="http://127.0.0.1:8080")
    ch.add_argument("--router", choices=["rule", "llm"], default="rule",
                    help="'llm' routes with a model over the published manifest; "
                         "'rule' is deterministic and needs no key")
    ch.add_argument("--model", default=None)
    ch.add_argument("--operator", default="teller1",
                    help="operator ALIAS to act as; the secret is resolved "
                         "server-side and never reaches the chatbot")
    ch.set_defaults(func=cmd_chat)

    db = sub.add_parser("dashboard", help="watch capabilities, runs and evidence")
    db.add_argument("--host", default="127.0.0.1")
    db.add_argument("--port", type=int, default=8082)
    db.add_argument("--dir", default="capabilities/meridian")
    db.add_argument("--evidence", action="append",
                    default=None, help="evidence root to read (repeatable)")
    db.add_argument("--api", default="http://127.0.0.1:8080",
                    help="capability API the dashboard invokes through; it is "
                         "an ordinary client and gets no special treatment")
    db.set_defaults(func=cmd_dashboard)

    pt = sub.add_parser("portal", help="signed-in console: dashboard + assistant "
                                       "behind one sign-in")
    pt.add_argument("action", nargs="?", default="serve",
                    choices=["serve", "init", "hash"],
                    help="serve the console; init writes the sign-in files; "
                         "hash prints a password hash to paste into them")
    pt.add_argument("--host", default="127.0.0.1")
    pt.add_argument("--port", type=int, default=8083)
    pt.add_argument("--api", default="http://127.0.0.1:8080")
    pt.add_argument("--dir", default="capabilities/meridian")
    pt.add_argument("--evidence", action="append", default=None,
                    help="evidence root to read (repeatable)")
    pt.add_argument("--handoffs", default="evidence/handoffs")
    pt.add_argument("--principals", default="config/principals.json",
                    help="who may sign in; never holds a Meridian credential")
    pt.add_argument("--session-key", default="config/session.key",
                    help="HMAC key the console and the API both read")
    pt.add_argument("--password", default=None, help="for `portal hash`")
    pt.add_argument("--router", choices=["rule", "llm"], default="rule")
    pt.add_argument("--model", default=None)
    pt.set_defaults(func=cmd_portal)

    g = sub.add_parser("codegen", help="emit a runnable Playwright script from an artifact")
    g.add_argument("--artifact", required=True)
    g.add_argument("--out", help="write to file (default: stdout)")
    g.set_defaults(func=cmd_codegen)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, "cmd", None) in ("dashboard", "portal") and \
            not getattr(args, "evidence", None):
        args.evidence = ["evidence/service", "evidence/meridian"]
    args.func(args)


if __name__ == "__main__":
    main()
