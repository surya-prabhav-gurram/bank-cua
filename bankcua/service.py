"""
Agent-facing capability API.

Exposes saved capabilities as callable-by-name endpoints so an AI agent can
discover a capability, read its typed contract, and invoke it with typed args --
without ever seeing the steps. Invocation runs the deterministic replay engine
and returns the structured result contract.

  GET  /capabilities            -> function-calling manifest (list)
  GET  /capabilities/<id>       -> full artifact (contract + steps)
  POST /invoke/<id>             -> run replay; body: {params, tenant?, allow_risky?,
                                   allow_unapproved?}; returns ReplayResult

Unapproved capabilities are refused by default (confidence/approval gate),
mirroring how an unattended agent should behave in production.
"""
from __future__ import annotations

import datetime as _dt
import os

from flask import Flask, jsonify, request

from .catalog import Catalog
from .observability.logging import RunLogger
from .replay.engine import ReplayEngine
from .safety.policy import Policy, PolicyEngine
from .surface.web_playwright import WebSurface
from .tenancy import TenantOverride, apply_overrides


def create_app(catalog_dir="capabilities", policy_path="config/policy.yaml",
               evidence_dir="evidence/service", base_url_override=None):
    app = Flask(__name__)
    cat = Catalog(catalog_dir)
    pol = (Policy.from_yaml(policy_path) if os.path.exists(policy_path)
           else Policy(allowed_url_patterns=["http://127.0.0.1:*", "http://localhost:*"]))

    @app.get("/capabilities")
    def capabilities():
        return jsonify(cat.manifest())

    @app.get("/capabilities/<cap_id>")
    def capability(cap_id):
        try:
            return app.response_class(cat.get(cap_id).to_json(),
                                      mimetype="application/json")
        except Exception:
            return jsonify({"error": "unknown capability"}), 404

    @app.post("/invoke/<cap_id>")
    def invoke(cap_id):
        body = request.get_json(force=True, silent=True) or {}
        params = body.get("params", {})
        try:
            art = cat.get(cap_id)
        except Exception:
            return jsonify({"error": "unknown capability"}), 404

        tenant = body.get("tenant")
        if tenant:
            ov = (TenantOverride.model_validate(tenant) if isinstance(tenant, dict)
                  else TenantOverride.load(tenant))
            art = apply_overrides(art, ov)
        if base_url_override:
            art.target.base_url = base_url_override
            art.target.allowed_url_patterns = [f"{base_url_override}/*", base_url_override]

        if art.approval_state.value != "approved" and not body.get("allow_unapproved"):
            return jsonify({"error": "capability not approved",
                            "approval_state": art.approval_state.value}), 409

        run_dir = os.path.join(
            evidence_dir, f"{cap_id}-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")
        logger = RunLogger(run_dir, "replay", art.secret_params(),
                           [str(params.get(n)) for n in art.secret_params()])
        pe = PolicyEngine(pol, artifact_url_patterns=art.target.allowed_url_patterns,
                          allow_risky_override=bool(body.get("allow_risky")))
        surf = WebSurface(art.target.base_url, headless=True)
        surf.start()
        res = None
        try:
            res = ReplayEngine(surf, pe, logger, None).run(art, params)
        finally:
            logger.finish(res.model_dump() if res else {})
            surf.stop()
        status = 200 if res.status.value in ("success", "business_outcome") else 422
        return app.response_class(res.model_dump_json(), mimetype="application/json"), status

    return app
