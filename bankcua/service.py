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
  POST /session/signon          -> establish the signed-in operator's session
  GET  /runs, /runs/<id>        -> run history and evidence, for the dashboard

One capability is not like the others
-------------------------------------
Signing an operator on to the host is not a service an agent should be able to
order up. It is what the console does at the door, once, on the alias the
sign-in names -- so `session_signon` in `config/service.yaml` names that
capability, and for any caller carrying a session it is withheld from the
manifest and refused at /invoke. `POST /session/signon` is the only way it runs,
and it runs the same way everything else does: same engine, same policy, same
evidence, same contract.

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
import time as _time
from dataclasses import dataclass, field

import yaml
from flask import Flask, jsonify, request, send_file

from .auth import (AuthError, Principal, SessionAuthority, may_invoke,
                   operator_alias_for, token_from_request)
from .catalog import Catalog
from .observability.logging import RunLogger
from .replay.engine import ReplayEngine
from .safety.credentials import (CredentialError, CredentialStore,
                                 EnvCredentialStore)
from .escalation.handoff import (HandoffCoordinator, HandoffStore,
                                 InterventionStatus)
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
    #: SIGN-IN roles permitted to invoke it: supervisor or teller. A separate
    #: question from `requires_role`, which is about the Meridian operator a run
    #: EXECUTES as. The two travel together on this deployment, since every
    #: principal acts as their own alias -- but `place_hold` constrains both,
    #: and collapsing them would make the narrower of the two unenforceable.
    #: Empty means the default in `auth.DEFAULT_PRINCIPAL_ROLES`.
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
    #: The capability the CONSOLE establishes when a person signs in, and which
    #: a signed-in session may therefore never invoke for itself.
    #:
    #: Signing an operator on to the host is not a thing anyone should ask for
    #: twice. It happens once, at the door, because somebody signed in -- so it
    #: is withheld from the published manifest and refused at /invoke for any
    #: caller carrying a session, and reachable only through
    #: `POST /session/signon`, which the console calls at sign-in. The model in
    #: front of the console never sees it, which is the point: a tool whose only
    #: possible effect is to redo what the sign-in already did is a tool that can
    #: only be called by mistake.
    #:
    #: Empty disables all of that and leaves the capability an ordinary one,
    #: which is what the direct agent path (no sign-in) still wants.
    session_signon: str = ""

    #: The branches this deployment recognises, offered at sign-in and used to
    #: validate the branch a session claims. Normalised to
    #: `[{"code": ..., "name": ...}]`; `code` is what MERIDIAN is sent and what
    #: is recorded on a run, `name` is only what the console displays.
    #:
    #: Server-side and in data for the same reason every other rule here is: a
    #: branch a caller could invent is an audit field a caller could invent,
    #: which is worse than no field at all. And these are the TARGET's branch
    #: codes -- a code the host does not offer is a run that fails at its first
    #: screen, so they are configured, never derived.
    #:
    #: Empty means this deployment offers no choice, and every run falls back to
    #: the operator's own configured branch.
    branches: list[dict] = field(default_factory=list)

    @property
    def branch_codes(self) -> list[str]:
        """Just the codes -- what a session's claim is validated against."""
        return [b["code"] for b in self.branches]

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
            session_signon=str(raw.get("session_signon") or ""),
            principals_path=raw.get("principals", "config/principals.json"),
            session_key_path=raw.get("session_key", "config/session.key"),
            branches=_branches(raw.get("branches")),
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


def _branches(raw) -> list[dict]:
    """Normalise the configured branch list.

    Accepts a bare code (`- MAIN-001`) or a mapping (`- {code, name}`), because
    the display name is a convenience and a deployment that does not want one
    should not have to write a mapping to say so. Entries with no code are
    dropped rather than becoming a blank option nobody can choose meaningfully.
    """
    out = []
    for entry in (raw or []):
        if isinstance(entry, dict):
            code = str(entry.get("code") or "").strip()
            name = str(entry.get("name") or "").strip()
        elif isinstance(entry, str):
            code, name = entry.strip(), ""
        else:
            # Anything else is a malformed entry, and `str()` would launder it
            # into a plausible-looking code -- a bare `-` in the YAML parses as
            # None, and `str(None)` is the string "None".
            continue
        if code:
            out.append({"code": code, "name": name})
    return out


def _is_safe_intervention_id(ident: str) -> bool:
    """Is this an identifier rather than a path?

    `HandoffStore` turns an id straight into `<root>/<id>.json`, and these
    routes match with `<path:...>` because intervention ids are long and dotted.
    That join is only safe while the id cannot climb out of the store.
    """
    return bool(ident) and ".." not in ident and "/" not in ident \
        and "\\" not in ident and not os.path.isabs(ident)


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
                "POST /session/signon": "establish the signed-in operator's "
                                        "session on the target",
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

        Same idea as `_service_supplied` below: never publish a tool a caller
        may not use. A teller's manifest does not describe `place_hold`, so the
        model routing for them cannot reach for it and be refused -- and the
        sign-on capability is withheld from everyone, because it is established
        at the door rather than invoked.
        """
        if principal is not None:
            if tool["name"] and tool["name"] == svc.session_signon:
                # Established at sign-in, so it is not in anyone's action space
                # here. Publishing it would offer a chatbot a tool whose only
                # possible effect is to redo what signing in already did -- and
                # the model has no way to know that, so it would eventually be
                # called, on a live host, for nothing.
                return None
            rule = svc.rule_for(tool["name"])
            if may_invoke(principal, rule.allowed_principal_roles) is not None:
                return None
        return tool

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
        entire action space, so a capability this operator may not use is best
        not described to the model routing for them in the first place.
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

    @app.get("/branches")
    def branches():
        """The branches this deployment recognises.

        Read by the console to build the sign-in dropdown. Published rather than
        hard-coded into the page for the same reason the operator list is: a
        deployment adds a branch by editing `config/service.yaml`, and the list
        a person is offered is then the same list the API validates against --
        so the console cannot offer a choice that /invoke will refuse.

        No secret and no authorisation lives here; branch codes are the sort of
        thing printed on a paying-in slip.
        """
        return jsonify(svc.branches)

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

        if principal is not None and svc.session_signon \
                and cap_id == svc.session_signon:
            # Signing the operator on is what the CONSOLE did when this person
            # signed in, on the same alias, against the same host. Doing it
            # again on request is at best a no-op that drives a live browser for
            # nothing, and at worst a way for whoever is in front of the console
            # to exercise an operator credential as an action of its own. It is
            # withheld from the manifest for the same reason; this is the half
            # that does not depend on anyone reading the manifest first.
            return _refused(
                "SIGNON_ESTABLISHED_AT_SIGN_IN",
                "the console sign-in, which establishes it once",
                f"{cap_id} runs when a person signs in to the console, not on "
                f"request; the session held by this caller is already the "
                f"result of it", cap_id, 403)

        if principal is not None:
            denial = may_invoke(principal, rule.allowed_principal_roles)
            if denial is not None:
                # Refused before a browser opens. A teller asking for a hold, or
                # a member asking for anything that is not theirs, never reaches
                # a member's account at all.
                return _refused(denial.code, denial.requirement, denial.reason,
                                cap_id, 403)

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
        branch, branch_refusal = _session_branch(principal)
        if branch_refusal is not None:
            code, requirement, reason = branch_refusal
            return _refused(code, requirement, reason, cap_id, 403)
        art, params, refusal = _bind(art, rule, alias, params,
                                     tenant=body.get("tenant"), branch=branch)
        if refusal is not None:
            return refusal

        # WHICH SURFACE asked. Not an authorisation input -- nothing is decided
        # by it -- but a routing one: it is stamped on any intervention the run
        # raises so the pause can be shown where the person who caused it is
        # sitting. Constrained to a known set so a caller cannot invent a
        # channel that no surface polls, which would hide the pause from
        # everyone.
        channel = str(body.get("channel") or "")
        if channel not in ("assistant", "dashboard", ""):
            channel = ""
        return _run(art, params, alias, principal, rule,
                    approver=str(body.get("approver", "")), channel=channel,
                    branch=params.get("branch", ""))

    # ---- runs paused for a person ---------------------------------------
    @app.get("/interventions")
    def interventions():
        """Runs currently stopped, waiting for someone.

        Exposed on the API rather than read off disk by each surface, because
        there is now more than one surface that has to show a pause. The
        dashboard reads the handoff store directly; the assistant cannot, and
        should not have to learn where that store lives to tell the person in
        front of it that their own request is waiting on them.

        Every signed-in subject on this console is staff, so there is no
        audience filter here -- the queue is the institution's own work.
        """
        try:
            principal = _principal()
        except AuthError as ex:
            return jsonify({"error": str(ex)}), 401
        out = []
        for req in HandoffStore(handoff_dir).list_open():
            dual = req.kind.value == "dual_control"
            out.append({
                "id": req.id, "kind": req.kind.value, "reason": req.reason,
                "capability_id": req.capability_id,
                "step": req.current_step_index,
                "channel": req.channel, "initiator": req.initiator,
                "initiator_role": req.initiator_role,
                "needs": "countersignature" if dual else "confirmation",
                "state_url": req.state_url,
                "created_at": req.created_at,
                "has_screenshot": bool(req.screenshot_path),
            })
        return jsonify(out)

    @app.get("/interventions/<path:req_id>/screenshot")
    def intervention_screenshot(req_id):
        """The frame captured when the run stopped.

        Approving an irreversible step without seeing it is a rubber stamp, so
        whatever surface offers the button has to be able to show the screen.
        """
        try:
            _principal()
        except AuthError as ex:
            return jsonify({"error": str(ex)}), 401
        if not _is_safe_intervention_id(req_id):
            return jsonify({"error": "unknown intervention"}), 404
        try:
            req = HandoffStore(handoff_dir).read(req_id)
        except Exception:
            return jsonify({"error": "unknown intervention"}), 404
        if not req.screenshot_path or not os.path.isfile(req.screenshot_path):
            return jsonify({"error": "no screenshot"}), 404
        return send_file(os.path.abspath(req.screenshot_path))

    @app.post("/interventions/<path:req_id>/confirm")
    def confirm_intervention(req_id):
        """Authorise a paused irreversible step, without driving the screen.

        The identity comes from the SESSION, never the body: an approver a
        caller can type is a string, not a second pair of eyes. A dual-control
        pause is refused here on purpose -- that one asks for an INDEPENDENT
        second person, and the engine re-checks independence itself, so letting
        it be cleared through the same button would quietly turn two signatures
        into one.
        """
        try:
            principal = _principal()
        except AuthError as ex:
            return _refused("SESSION_INVALID", "a valid console sign-in",
                            str(ex), "", 401)
        if principal is None:
            return _refused(
                "SESSION_REQUIRED", "a signed-in supervisor",
                "confirming an irreversible step records WHO approved it, so "
                "there has to be somebody signed in", "", 401)
        if principal.role != "supervisor":
            return _refused(
                "CONFIRMATION_NOT_PERMITTED_FOR_ROLE", "a supervisor sign-in",
                f"{principal.username!r} is signed in as {principal.role!r}; "
                f"authorising an irreversible step is supervisor work", "", 403)
        if not _is_safe_intervention_id(req_id):
            return jsonify({"error": "unknown intervention"}), 404
        store = HandoffStore(handoff_dir)
        try:
            req = store.read(req_id)
        except Exception:
            return jsonify({"error": "unknown intervention"}), 404
        if req.kind.value == "dual_control":
            return jsonify({"error": "this pause needs an independent "
                                     "counter-signature from a second "
                                     "supervisor, not a confirmation"}), 409
        if req.status.value != "open":
            return jsonify({"error": f"already {req.status.value}"}), 409
        req.status = InterventionStatus.RESOLVED
        req.resolved_at = _time.time()
        req.resolved_by = principal.username
        req.resume = True
        req.controller = "agent"
        # Deliberately not the word "manual": the engine reads this note to
        # decide whether the operator already performed the step by hand. Nobody
        # touched the page here -- the automation still posts it.
        req.resolution_note = (f"confirmed by {principal.username} from "
                               f"{req.channel or 'the operator console'}; the "
                               f"automation performed the step")
        store.write(req)
        return jsonify({"ok": True, "resolved_by": principal.username})

    @app.post("/session/signon")
    def session_signon():
        """Establish the signed-in operator's session on the target. Once.

        The other half of withholding `session_signon` from every signed-in
        caller. The capability still runs -- same replay engine, same policy
        engine, same evidence directory, same result contract -- it is simply
        not something the person or the model in front of the console can ask
        for. It happens BECAUSE they signed in, which is the only moment at
        which signing on means anything.

        Staff only, and not because a member is less trusted: a member has no
        Meridian operator identity at all, so there is no operator session to
        establish on their behalf. Their work runs delegated on the alias the
        deployment configured, and exercising that alias's credential is not
        something a member's sign-in should cause.
        """
        if not svc.session_signon:
            return jsonify({
                "error": "this deployment establishes no sign-on at sign-in; "
                         "set `session_signon` in the service config"}), 404
        cap_id = svc.session_signon
        try:
            principal = _principal()
        except AuthError as ex:
            return _refused("SESSION_INVALID", "a valid console sign-in",
                            str(ex), cap_id, 401)
        if principal is None:
            return _refused(
                "SESSION_REQUIRED", "a signed-in console session",
                "a sign-on is established for the person who signed in, so "
                "there has to be one", cap_id, 401)
        try:
            art = cat.get(cap_id)
        except Exception:
            return jsonify({"error": "unknown capability"}), 404

        rule = svc.rule_for(cap_id)
        denial = may_invoke(principal, rule.allowed_principal_roles)
        if denial is not None:
            return _refused(denial.code, denial.requirement, denial.reason,
                            cap_id, 403)
        alias = operator_alias_for(principal, svc.default_operator)
        if not alias:
            return _refused("OPERATOR_REQUIRED",
                            "an operator alias the service can resolve",
                            f"{principal.username!r} maps to no operator alias",
                            cap_id, 403)
        branch, branch_refusal = _session_branch(principal)
        if branch_refusal is not None:
            # Refused at the door rather than signed on at a branch nobody
            # configured. MERIDIAN's own sign-on screen has a branch field, so
            # this is the run that would type it in.
            code, requirement, reason = branch_refusal
            return _refused(code, requirement, reason, cap_id, 403)
        art, params, refusal = _bind(art, rule, alias, {}, branch=branch)
        if refusal is not None:
            # Notably including CAPABILITY_NOT_APPROVED. A deployment that has
            # not approved its sign-on capability has not approved it for this
            # either -- the console reports the refusal and lets the person in
            # unverified rather than quietly making an exception for itself.
            return refusal
        return _run(art, params, alias, principal, rule,
                    branch=params.get("branch", ""))

    def _session_branch(principal):
        """The branch this run is performed FROM, or a refusal. Three cases.

          * No principal -- the direct agent path, where nobody signed in and
            nobody chose. Returns "", and the operator's own configured branch
            (`identity.context`) stands, exactly as it did before sign-in
            existed.
          * A principal carrying a branch this deployment lists -- that branch,
            which then overrides the operator's configured one for this run.
          * A principal carrying anything else -- REFUSED. This includes the
            case where the deployment lists no branches at all: if no choice was
            on offer, a token that claims one is not describing something that
            happened. An audit field the caller can choose freely is worse than
            no audit field, because it looks like evidence.

        This is the check that lets the branch ride in the session token at all:
        the claim is re-validated against server-side configuration on every
        invocation, so forging it buys a refusal rather than a free-text entry
        in somebody else's audit trail.
        """
        branch = (principal.branch if principal is not None else "") or ""
        if not branch:
            return "", None
        if branch not in svc.branch_codes:
            return "", ("BRANCH_NOT_CONFIGURED",
                        (f"a branch this deployment recognises: "
                         f"{', '.join(svc.branch_codes)}" if svc.branches
                         else "no branch, since this deployment configures none"),
                        f"{branch!r} is not a configured branch")
        return branch, None

    # ---- the two halves every invocation shares -------------------------
    def _bind(art, rule, alias, params, tenant=None, branch=""):
        """Authorise the OPERATOR and bind the capability's parameters.

        Split out from the route because the console's sign-on has to go through
        exactly this, and a second, quieter path for the one capability that
        exercises a credential is precisely the shortcut this service exists not
        to have. Returns `(artifact, params, None)` or `(_, _, refusal)`.
        """
        cap_id = art.id
        try:
            identity = creds.resolve(alias)
        except CredentialError as ex:
            return art, params, _refused(
                "UNKNOWN_OPERATOR", "a configured operator alias",
                str(ex), cap_id, 403)

        if rule.allowed_operators and alias not in rule.allowed_operators:
            return art, params, _refused(
                "OPERATOR_NOT_PERMITTED",
                f"an operator on this capability's allow list "
                f"({rule.allowed_operators})",
                f"{alias!r} is not permitted to invoke {cap_id}", cap_id, 403)

        if rule.requires_role and identity.role != rule.requires_role:
            # The application enforces this too, and would refuse at its own
            # screen. Refusing here as well means we never drive a member's
            # account up to a wall we already knew was there.
            return art, params, _refused(
                "ROLE_NOT_PERMITTED",
                f"an operator with role {rule.requires_role!r}",
                f"{alias!r} holds role {identity.role!r}", cap_id, 403)

        if art.approval_state.value != "approved" and not rule.allow_unapproved:
            return art, params, _refused(
                "CAPABILITY_NOT_APPROVED",
                "the capability to be approved for unattended use",
                f"{cap_id} is {art.approval_state.value}", cap_id, 409)

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
        params = dict(params)
        params["operator"] = alias
        params.update(identity.context)
        if branch:
            # The branch chosen at sign-in beats the operator's configured one,
            # and is applied after `identity.context` for that reason. It has
            # already been validated against `svc.branches`; it arrives here as
            # a value the deployment recognises, never as something a request
            # supplied. Secrets are merged last regardless, so no branch can
            # displace a credential.
            params["branch"] = branch
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
            return art, params, _refused(
                "MISSING_REQUIRED_INPUT",
                f"a value for each required input: {missing}",
                f"no value supplied for {', '.join(missing)}", cap_id, 400)
        return art, params, None

    def _run(art, params, alias, principal, rule, approver: str = "",
             channel: str = "", branch: str = ""):
        """Execute one authorised capability run and shape the response.

        Everything above this point decided WHETHER the call may happen; this is
        the part that happens. Both entry points -- an agent at /invoke and the
        console establishing a sign-on -- land here, so a run's evidence, its
        policy engine, its handoff coordinator and its result contract cannot
        differ depending on which door it came through.
        """
        cap_id = art.id
        run_id = (f"{cap_id}-"
                  f"{_dt.datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:-3]}")
        run_dir = os.path.join(evidence_dir, run_id)
        os.makedirs(run_dir, exist_ok=True)
        if principal is not None:
            # Who asked and WHERE FROM, recorded ALONGSIDE the evidence rather
            # than inside the result contract: the contract describes what the
            # bank answered, and a subject is not part of that answer. This is
            # what an auditor reads to see which sign-in stood behind an action,
            # which Meridian operator it ran as, and which branch it was
            # performed from.
            #
            # `branch` is passed rather than read off the principal so that the
            # value recorded is the one actually SENT to the host -- including
            # the fallback to the operator's configured branch on a session that
            # chose none. Evidence that can disagree with what happened is not
            # evidence.
            with open(os.path.join(run_dir, "principal.json"), "w") as fh:
                json.dump({**principal.to_public(), "runs_as": alias,
                           "branch": branch or principal.branch}, fh, indent=2)
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
                               approver=approver,
                               ledger=ledger, channel=channel,
                               initiator_role=(principal.role if principal
                                               else "")).run(art, params)
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
            # Who asked for it, which operator alias it ran as, and which
            # branch it was performed from -- if anyone signed in. Runs made
            # from the CLI have no subject, and that absence is meaningful: an
            # unattributable run is exactly what an auditor wants to notice.
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
