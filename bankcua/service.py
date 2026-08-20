"""
Agent-facing capability API.

A calling agent discovers a capability, reads its typed contract, and invokes it
by name with typed args -- without ever seeing the UI, the steps, or a password.
Each invocation runs the deterministic replay engine and returns the structured
result contract.

  GET  /capabilities            -> function-calling manifest
  GET  /capabilities/<id>       -> full contract (inputs, outputs, steps)
  GET  /operators               -> alias names only, never secrets
  POST /invoke/<id>             -> {params, operator} -> ReplayResult
  GET  /runs, /runs/<id>        -> run history and evidence, for the dashboard

The decision this file exists to enforce
----------------------------------------
Wrapping capabilities in an API is where a safety model usually dies. The
previous version accepted `allow_risky` and `allow_unapproved` IN THE REQUEST
BODY, which meant the caller decided whether irreversible actions were permitted
and whether the approval gate applied. Put a chatbot in front of that and a
language model is choosing whether to authorise a funds transfer. It also took a
password per call, so the caller chose which operator to be -- a teller-level
caller could name a supervisor and the system would sign on as them, leaving the
application's own authorisation boundary intact while we walked around it.

So every one of those decisions is now SERVER-SIDE, in
`config/service.yaml`:

  * whether a capability may perform its irreversible step,
  * whether an unapproved capability may run at all,
  * which operator role each capability requires,
  * and where the operator's secret comes from (never the request).

A caller supplies an operator ALIAS and typed args. It cannot escalate its own
privilege, because nothing it sends is consulted when deciding what it may do.

The seam: swap the transport (HTTP here; MCP or a queue elsewhere) without
touching the engine or the result contract -- `invoke_capability` below is the
whole surface, and it takes plain data.
"""
from __future__ import annotations

import datetime as _dt
import glob
import json
import os
from dataclasses import dataclass, field

import yaml
from flask import Flask, jsonify, request

from .auth import (AuthError, Principal, SessionAuthority, may_invoke,
                   operator_alias_for, scope_params, token_from_request)
from .catalog import Catalog
from .observability.logging import RunLogger
from .replay.engine import ReplayEngine
from .safety.credentials import (CredentialError, CredentialStore,
                                 EnvCredentialStore)
from .escalation.handoff import HandoffCoordinator, HandoffStore
from .safety.ledger import Ledger
from .safety.policy import Policy, PolicyEngine
from .surface.web_playwright import WebSurface
from .tenancy import TenantOverride, apply_overrides


@dataclass
class CapabilityRule:
    """What the SERVICE permits for one capability. Never caller-supplied."""
    allow_risky: bool = False
    allow_unapproved: bool = False
    requires_role: str = ""
    #: Aliases permitted to invoke this capability at all. Empty means any
    #: configured alias whose role satisfies `requires_role`.
    allowed_operators: list[str] = field(default_factory=list)
    #: SIGN-IN roles permitted to invoke it: supervisor, teller, member. A
    #: separate question from `requires_role`, which is about the Meridian
    #: operator a run EXECUTES as -- a member's work runs delegated as a teller
    #: alias, so without this check every member would inherit a teller's action
    #: space the moment they signed in. Empty means the closed default
    #: (`auth.DEFAULT_PRINCIPAL_ROLES`: staff only).
    allowed_principal_roles: list[str] = field(default_factory=list)


@dataclass
class ServiceConfig:
    """Server-side authorisation, loaded from data so it can be reviewed.

    A reviewer should be able to answer "what is this deployment allowed to do?"
    by reading one file, without tracing request handling.
    """
    default_operator: str = ""
    rules: dict[str, CapabilityRule] = field(default_factory=dict)
    #: Whether an invocation must carry a signed-in subject. False keeps the
    #: direct agent path (operator alias in the body) working; the browser
    #: console starts the API with it on.
    require_session: bool = False
    principals_path: str = "config/principals.json"
    session_key_path: str = "config/session.key"

    @classmethod
    def from_yaml(cls, path: str) -> "ServiceConfig":
        if not os.path.exists(path):
            # Fail closed: with no configuration, nothing risky and nothing
            # unapproved may run. An absent config must not mean "allow".
            return cls()
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
        return cls(
            default_operator=raw.get("default_operator", ""),
            require_session=bool(raw.get("require_session", False)),
            principals_path=raw.get("principals", "config/principals.json"),
            session_key_path=raw.get("session_key", "config/session.key"),
            rules={cid: CapabilityRule(
                allow_risky=r.get("allow_risky", False),
                allow_unapproved=r.get("allow_unapproved", False),
                requires_role=r.get("requires_role", ""),
                allowed_operators=list(r.get("allowed_operators") or []),
                allowed_principal_roles=list(
                    r.get("allowed_principal_roles") or []))
                for cid, r in (raw.get("capabilities") or {}).items()},
        )

    def rule_for(self, cap_id: str) -> CapabilityRule:
        return self.rules.get(cap_id, CapabilityRule())


def _refused(code: str, requirement: str, reason: str, cap_id: str, status: int):
    """A guardrail declining is not a server fault, so it is shaped like the
    engine's own refusal rather than like an error blob. The caller branches on
    one contract whether the refusal came from the service or from replay."""
    return jsonify({
        "status": "refused", "capability_id": cap_id, "outputs": {},
        "refusal": {"code": code, "requirement": requirement, "reason": reason},
    }), status


def create_app(catalog_dir="capabilities/meridian",
               policy_path="config/policy.meridian.yaml",
               service_config_path="config/service.yaml",
               evidence_dir="evidence/service",
               credential_store: CredentialStore | None = None,
               handoff_dir: str = "evidence/handoffs",
               cdp_port: int = 9222,
               handoff_timeout_s: float = 90.0,
               base_url_override=None,
               require_session: bool | None = None,
               session_authority: SessionAuthority | None = None):
    app = Flask(__name__)
    # A grid's column order is part of its meaning -- "Share ID, Type,
    # Balance, Status" is how the operator reads the screen. Flask sorts
    # JSON keys by default, which silently alphabetises every extracted
    # table on the way out.
    app.json.sort_keys = False
    cat = Catalog(catalog_dir)
    pol = (Policy.from_yaml(policy_path) if os.path.exists(policy_path)
           else Policy(allowed_url_patterns=["http://127.0.0.1:*"]))
    svc = ServiceConfig.from_yaml(service_config_path)
    creds = credential_store or EnvCredentialStore()
    # WHO is calling, as opposed to WHAT identity the work runs as. The console
    # signs a person in; this re-resolves their token against the principal
    # store on every request, so nothing about role or member scope is taken
    # from the token itself. An explicit argument wins over the config file so
    # `portal` can require sessions without editing a deployment's YAML.
    auth = session_authority or SessionAuthority(
        principals_path=svc.principals_path,
        secret_path=svc.session_key_path)
    sessions_required = (svc.require_session if require_session is None
                         else bool(require_session))
    # One ledger for the process: the velocity budget belongs to the institution,
    # not to a request. An unattended agent looping on /invoke is exactly the
    # shape a per-invocation ceiling cannot see.
    ledger = Ledger(os.path.join(evidence_dir, "value_ledger.jsonl"))
    os.makedirs(evidence_dir, exist_ok=True)

    @app.get("/")
    def index():
        """A reviewer's first request is usually to the root. Answering with a
        map beats a 404 that looks like the service is broken."""
        return jsonify({
            "service": "bank-cua capability API",
            "catalog": catalog_dir,
            "authorisation": f"server-side, from {service_config_path}",
            "endpoints": {
                "GET /capabilities": "function-calling manifest",
                "GET /capabilities/<id>": "full typed contract",
                "GET /operators": "operator aliases and roles (never secrets)",
                "POST /invoke/<id>": "{params, operator} -> ReplayResult",
                "GET /runs": "run history",
                "GET /runs/<run_id>": "one run with its evidence",
            },
        })

    def _principal() -> Principal | None:
        """The signed-in subject behind this request, if there is one.

        Raises `AuthError` rather than returning None on a BAD token: a token
        that does not verify is a different event from no token at all, and
        answering both with "carry on as an anonymous agent" is how a revoked
        session keeps working.
        """
        token = token_from_request(request.headers, request.cookies)
        if not token:
            return None
        return auth.principal(token)

    def _quiet_principal() -> Principal | None:
        """For read-only discovery endpoints, where a bad token should narrow
        what is shown rather than fail the page."""
        try:
            return _principal()
        except AuthError:
            return None

    def _visible(tool: dict, principal: Principal | None) -> dict | None:
        """One manifest entry as this principal may see it, or None.

        Two things happen here, and both are the same idea as
        `_service_supplied` below: never publish a tool a caller may not use,
        and never ask a caller for an argument it does not get to choose. For a
        member, `member_id` is one of those arguments -- it comes from their
        sign-in -- so it is removed from the schema rather than left there for a
        chatbot to fill in with somebody else's number.
        """
        if principal is not None:
            rule = svc.rule_for(tool["name"])
            if may_invoke(principal, rule.allowed_principal_roles) is not None:
                return None
        if principal is None or not principal.is_member:
            return tool
        shaped = json.loads(json.dumps(tool))
        schema = shaped.get("input_schema") or {}
        for name in ("member_id", "member_number", "mid"):
            (schema.get("properties") or {}).pop(name, None)
            if name in (schema.get("required") or []):
                schema["required"] = [r for r in schema["required"] if r != name]
        return shaped

    def _service_supplied() -> set[str]:
        """Parameter names this deployment fills in from the operator's identity,
        so the manifest never asks a caller for something it cannot know."""
        names = {"operator"}
        for alias in creds.aliases():
            try:
                identity = creds.resolve(alias)
            except CredentialError:
                continue
            names |= set(identity.context) | set(identity.secrets)
        return names

    @app.get("/capabilities")
    def capabilities():
        """The manifest, narrowed to what the caller may actually invoke.

        Narrowing here is defence in depth, not the defence itself -- /invoke
        refuses the same calls independently. But the manifest IS a chatbot's
        entire action space, so a capability a member may not use is best not
        described to the model routing for them in the first place.
        """
        principal = _quiet_principal()
        tools = [_visible(t, principal)
                 for t in cat.manifest(supplied_by_service=_service_supplied())]
        return jsonify([t for t in tools if t is not None])

    @app.get("/session")
    def session():
        """Who the presented token says is signed in, re-resolved live.

        The console reads this to render its header. It returns no secret and
        no operator password -- a Principal never holds one.
        """
        try:
            principal = _principal()
        except AuthError as ex:
            return jsonify({"error": str(ex)}), 401
        if principal is None:
            return jsonify({"error": "not signed in"}), 401
        return jsonify({
            **principal.to_public(),
            "runs_as": operator_alias_for(principal, svc.default_operator),
            "capabilities": [t["name"] for t in
                             (_visible(t, principal) for t in cat.manifest(
                                 supplied_by_service=_service_supplied()))
                             if t is not None],
        })

    @app.get("/capabilities/<cap_id>")
    def capability(cap_id):
        try:
            return app.response_class(cat.get(cap_id).to_json(),
                                      mimetype="application/json")
        except Exception:
            return jsonify({"error": "unknown capability"}), 404

    @app.get("/operators")
    def operators():
        """Alias names and roles. No secret is reachable through this API at all,
        which is what lets a chatbot offer a choice of operator safely."""
        principal = _quiet_principal()
        out = []
        for alias in creds.aliases():
            try:
                out.append({"alias": alias, "role": creds.resolve(alias).role})
            except CredentialError:
                continue
        if principal is not None:
            # Once someone is signed in, the choice is gone: their work runs as
            # the alias their sign-in maps to. Returning the full list anyway
            # would put a "run as super1" option in front of a teller that the
            # service is only going to refuse -- and in front of a member, it
            # would disclose the institution's operator names for nothing.
            runs_as = operator_alias_for(principal, svc.default_operator)
            out = [o for o in out if o["alias"] == runs_as]
        return jsonify(out)

    @app.get("/runs")
    def runs():
        return jsonify(_run_index(evidence_dir))

    @app.get("/runs/<run_id>")
    def run_detail(run_id):
        detail = _run_detail(evidence_dir, run_id)
        if detail is None:
            return jsonify({"error": "unknown run"}), 404
        return jsonify(detail)

    @app.post("/invoke/<cap_id>")
    def invoke(cap_id):
        body = request.get_json(force=True, silent=True) or {}
        params = dict(body.get("params") or {})
        try:
            art = cat.get(cap_id)
        except Exception:
            return jsonify({"error": "unknown capability"}), 404

        rule = svc.rule_for(cap_id)

        # ---- subject: WHO is asking. Established before anything else,
        # because both of the checks that follow -- may this role use this
        # capability, and whose records may it touch -- are properties of the
        # person, not of the request.
        try:
            principal = _principal()
        except AuthError as ex:
            return _refused("SESSION_INVALID", "a valid console sign-in",
                            str(ex), cap_id, 401)
        if principal is None and sessions_required:
            return _refused(
                "SESSION_REQUIRED", "a signed-in console session",
                "this deployment requires every invocation to name a "
                "signed-in person", cap_id, 401)

        if principal is not None:
            denial = may_invoke(principal, rule.allowed_principal_roles)
            if denial is not None:
                # Refused before a browser opens. A teller asking for a hold, or
                # a member asking for anything that is not theirs, never reaches
                # a member's account at all.
                return _refused(denial.code, denial.requirement, denial.reason,
                                cap_id, 403)
            params, denial = scope_params(principal, params)
            if denial is not None:
                return _refused(denial.code, denial.requirement, denial.reason,
                                cap_id, 403)
            if principal.is_member and (body.get("approver")
                                        or body.get("tenant")):
                # Two request fields that are not a member's to send.
                #
                # `approver` clears the dual-control gate on a transfer over the
                # threshold. The engine checks that a counter-signature is
                # INDEPENDENT of the initiator -- but a member's work is
                # initiated by the delegated teller alias, so naming a
                # supervisor here would look independent and would authorise a
                # member's own large transfer with nobody having approved it.
                # `tenant` re-points the capability at another deployment of the
                # vendor product, which is an integration decision.
                #
                # Both were always caller-supplied, which was defensible while
                # every caller was the institution. It stops being defensible
                # the moment a member of the public is one.
                return _refused(
                    "FIELD_NOT_PERMITTED_FOR_ROLE",
                    "a staff sign-in for approver or tenant fields",
                    "a member sign-in cannot counter-sign its own request or "
                    "re-point the capability at another tenant", cap_id, 403)

        # ---- identity: named by alias, resolved here, never sent by the caller
        alias = str(body.get("operator") or svc.default_operator or "")
        if principal is not None:
            # The sign-in decides, not the body. Otherwise a teller's session
            # could name `super1` and the service would faithfully sign on as
            # one -- the exact privilege escalation the credential store exists
            # to close, reopened one layer up.
            session_alias = operator_alias_for(principal, svc.default_operator)
            if body.get("operator") and str(body["operator"]) != session_alias:
                return _refused(
                    "OPERATOR_NOT_SESSION",
                    f"invocations to run as {session_alias!r}",
                    f"{principal.username!r} is signed in and acts as "
                    f"{session_alias!r}; the request asked to act as "
                    f"{str(body['operator'])!r}", cap_id, 403)
            alias = session_alias
        if not alias:
            return _refused("OPERATOR_REQUIRED",
                            "an operator alias the service can resolve",
                            "no operator supplied and no default configured",
                            cap_id, 403)
        try:
            identity = creds.resolve(alias)
        except CredentialError as ex:
            return _refused("UNKNOWN_OPERATOR", "a configured operator alias",
                            str(ex), cap_id, 403)

        if rule.allowed_operators and alias not in rule.allowed_operators:
            return _refused(
                "OPERATOR_NOT_PERMITTED",
                f"an operator on this capability's allow list "
                f"({rule.allowed_operators})",
                f"{alias!r} is not permitted to invoke {cap_id}", cap_id, 403)

        if rule.requires_role and identity.role != rule.requires_role:
            # The application enforces this too, and would refuse at its own
            # screen. Refusing here as well means we never drive a member's
            # account up to a wall we already knew was there.
            return _refused(
                "ROLE_NOT_PERMITTED",
                f"an operator with role {rule.requires_role!r}",
                f"{alias!r} holds role {identity.role!r}", cap_id, 403)

        if art.approval_state.value != "approved" and not rule.allow_unapproved:
            return _refused(
                "CAPABILITY_NOT_APPROVED",
                "the capability to be approved for unattended use",
                f"{cap_id} is {art.approval_state.value}", cap_id, 409)

        tenant = body.get("tenant")
        if tenant:
            ov = (TenantOverride.model_validate(tenant) if isinstance(tenant, dict)
                  else TenantOverride.load(tenant))
            art = apply_overrides(art, ov)
        if base_url_override:
            art.target.base_url = base_url_override
            art.target.allowed_url_patterns = [f"{base_url_override}/*",
                                               base_url_override]

        # Identity is merged in AFTER everything the caller sent, so a caller
        # cannot override a secret -- or a branch -- by sending a param of the
        # same name.
        params["operator"] = alias
        params.update(identity.context)
        params.update(identity.secrets)

        # The capability's typed contract, enforced at the boundary. Replay
        # raises on a missing required input, and an exception here escapes as a
        # 500 HTML page -- the one shape this API promises never to return, since
        # a caller cannot branch on it. It also launches a browser for a call
        # that could never have run. A missing argument is a caller error, so it
        # refuses like any other guardrail: same contract, nothing opened.
        missing = [p.name for p in art.inputs
                   if p.required and p.name not in params]
        if missing:
            return _refused(
                "MISSING_REQUIRED_INPUT",
                f"a value for each required input: {missing}",
                f"no value supplied for {', '.join(missing)}", cap_id, 400)

        run_id = (f"{cap_id}-"
                  f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}")
        run_dir = os.path.join(evidence_dir, run_id)
        os.makedirs(run_dir, exist_ok=True)
        if principal is not None:
            # Who asked, recorded ALONGSIDE the evidence rather than inside the
            # result contract: the contract describes what the bank answered,
            # and a subject is not part of that answer. It is what lets the
            # console show a member their own runs and nobody else's, and what
            # an auditor reads to see which sign-in stood behind an action.
            with open(os.path.join(run_dir, "principal.json"), "w") as fh:
                json.dump({**principal.to_public(), "runs_as": alias}, fh,
                          indent=2)
        logger = RunLogger(run_dir, "replay", art.secret_params(),
                           {n: str(params[n]) for n in art.secret_params()
                            if params.get(n)})
        pe = PolicyEngine(pol,
                          artifact_url_patterns=art.target.allowed_url_patterns,
                          allow_risky_override=rule.allow_risky)
        # The browser is launched with a CDP endpoint so that a step gated for
        # human confirmation can be handed to an operator ON THE SAME SESSION --
        # the pause-and-escalate path has to survive being wrapped in an API, or
        # the wrapper has quietly removed it. One port means one gated run at a
        # time, which is a demo constraint and is stated in the README.
        surf = WebSurface(art.target.base_url, headless=True, cdp_port=cdp_port)
        surf.start()
        coordinator = HandoffCoordinator(HandoffStore(handoff_dir), logger,
                                         wait_timeout_s=handoff_timeout_s)
        res = None
        try:
            res = ReplayEngine(surf, pe, logger, coordinator,
                               initiator=alias,
                               approver=str(body.get("approver", "")),
                               ledger=ledger).run(art, params)
        finally:
            logger.finish(json.loads(res.model_dump_json()) if res else {})
            surf.stop()

        # A refusal is a decision, not a fault: the caller must change the
        # REQUEST, so it maps to 403 rather than the 422 used for a run that
        # actually broke. A business outcome is a successful call with a
        # non-happy answer, so it stays 200.
        status = {"success": 200, "business_outcome": 200,
                  "refused": 403, "escalated": 409}.get(res.status.value, 422)
        payload = json.loads(res.model_dump_json())
        payload["run_id"] = run_id
        payload["operator"] = alias
        if principal is not None:
            payload["principal"] = principal.to_public()
        return jsonify(payload), status

    return app


# ---------------------------------------------------------------------------
# Run history. A read-only projection of the evidence the engine already writes:
# the dashboard never gets its own store, so there is nothing to fall out of sync
# with what actually happened.
# ---------------------------------------------------------------------------
def _run_index(evidence_dir: str) -> list[dict]:
    """Index every run under a directory, discovery and replay alike.

    A discovery summary has a different shape from a replay result -- it records
    how a capability was FOUND, not what an invocation returned. Both are runs a
    reviewer wants to see, so the shapes are normalised here rather than the
    dashboard learning about two of them, and the `kind` field keeps them
    distinguishable instead of blended.
    """
    out = []
    for summary_path in glob.glob(os.path.join(evidence_dir, "*", "summary.json")):
        run_dir = os.path.dirname(summary_path)
        run_id = os.path.basename(run_dir)
        try:
            with open(summary_path) as fh:
                summary = json.load(fh)
        except Exception:
            continue
        is_discovery = "artifact_path" in summary or run_id.startswith("discovery-")
        outcome = (summary.get("business_outcome") or summary.get("refusal")
                   or summary.get("failure") or {})
        capability = summary.get("capability_id") or ""
        if not capability and is_discovery:
            capability = run_id.replace("discovery-", "").split("-anthropic")[0]
        out.append({
            "run_id": run_id,
            "kind": "discovery" if is_discovery else "replay",
            # Who asked for it, if anyone signed in did. Runs made from the CLI
            # have no subject, and that absence is meaningful: the console shows
            # them to staff and withholds them from members, because a run
            # nobody can attribute is not a run to show a member of the public.
            "principal": _read_principal(run_dir),
            "capability_id": capability,
            "status": summary.get("status", "unknown"),
            "code": (outcome or {}).get("code", "") or summary.get("reason", "")[:60],
            "steps_executed": summary.get("steps_executed",
                                          summary.get("num_steps", 0)),
            "duration_s": summary.get("duration_s", 0),
            "started_at": os.path.getmtime(run_dir),
            "outputs": summary.get("outputs", {}),
        })
    return sorted(out, key=lambda r: r["started_at"], reverse=True)


def _read_principal(run_dir: str) -> dict:
    """The sign-in recorded next to a run's evidence, or {}."""
    path = os.path.join(run_dir, "principal.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _read_json(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _run_detail(evidence_dir: str, run_id: str) -> dict | None:
    run_dir = os.path.join(evidence_dir, run_id)
    summary_path = os.path.join(run_dir, "summary.json")
    if not os.path.isdir(run_dir) or not os.path.exists(summary_path):
        return None
    events = []
    log_path = os.path.join(run_dir, "run.jsonl")
    if os.path.exists(log_path):
        with open(log_path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue
    return {
        "run_id": run_id,
        "summary": _read_json(summary_path),
        "principal": _read_principal(run_dir),
        "events": events,
        "evidence": sorted(os.path.basename(p) for p in glob.glob(
            os.path.join(run_dir, "*")) if not p.endswith(("summary.json",
                                                           "run.jsonl",
                                                           "principal.json"))),
    }
