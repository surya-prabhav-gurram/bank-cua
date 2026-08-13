"""
End-to-end replay integration tests against the live mock app.

Exercises the result contract: SUCCESS (+typed outputs), BUSINESS_OUTCOME
(not-found / permission), and a HARD_FAILURE (session timeout injected).
Skips cleanly if Playwright or the mock can't run.
"""
import os
import urllib.request

import pytest

pytest.importorskip("playwright")

from bankcua.schema import CapabilityArtifact
from bankcua.replay.engine import ReplayEngine
from bankcua.replay.result import ReplayStatus
from bankcua.observability.logging import RunLogger
from bankcua.safety.policy import Policy, PolicyEngine
from bankcua.surface.web_playwright import WebSurface

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
ART = os.path.join(ROOT, "capabilities", "corebank.member_savings_lookup.json")
CREDS = {"username": "operator", "password": "password123"}


def _run(mock_app, params, tmp_path, name, allow_risky=False):
    if not os.path.exists(ART):
        pytest.skip("artifact not present; run scripts/run_discovery.sh first")
    art = CapabilityArtifact.from_json(open(ART).read())
    art.target.base_url = mock_app
    pol = Policy(allowed_url_patterns=["http://127.0.0.1:*"])
    pe = PolicyEngine(pol, allow_risky_override=allow_risky)
    logger = RunLogger(str(tmp_path / name), "replay", art.secret_params(),
                       [params.get(n, "") for n in art.secret_params()])
    surf = WebSurface(art.target.base_url, headless=True)
    surf.start()
    try:
        return ReplayEngine(surf, pe, logger, None).run(art, params)
    finally:
        surf.stop()


def _control(mock, k, v=""):
    urllib.request.urlopen(f"{mock}/_control/set?key={k}&value={v}").read()


def _reset(mock):
    urllib.request.urlopen(f"{mock}/_control/reset").read()


def test_replay_success(mock_app, tmp_path):
    _reset(mock_app)
    r = _run(mock_app, {**CREDS, "member_id": "12345"}, tmp_path, "ok")
    assert r.status == ReplayStatus.SUCCESS
    assert r.outputs["member_name"] == "Jane A. Doe"
    assert r.outputs["savings_balance"] == 421355   # money -> cents


def test_replay_not_found_is_business_outcome(mock_app, tmp_path):
    _reset(mock_app)
    r = _run(mock_app, {**CREDS, "member_id": "00000"}, tmp_path, "nf")
    assert r.status == ReplayStatus.BUSINESS_OUTCOME
    assert r.business_outcome.code == "MEMBER_NOT_FOUND"
    assert r.failure is None


def test_replay_permission_denied(mock_app, tmp_path):
    _reset(mock_app)
    r = _run(mock_app, {**CREDS, "member_id": "99999"}, tmp_path, "perm")
    assert r.status == ReplayStatus.BUSINESS_OUTCOME
    assert r.business_outcome.code == "PERMISSION_DENIED"


def test_replay_session_timeout_is_hard_failure(mock_app, tmp_path):
    _reset(mock_app)
    _control(mock_app, "timeout", "on")
    try:
        r = _run(mock_app, {**CREDS, "member_id": "12345"}, tmp_path, "to")
    finally:
        _reset(mock_app)
    assert r.status == ReplayStatus.FAILURE
    assert r.failure.code == "SESSION_TIMEOUT"


def test_replay_interstitial_recovers(mock_app, tmp_path):
    _reset(mock_app)
    _control(mock_app, "inject", "interstitial")
    try:
        r = _run(mock_app, {**CREDS, "member_id": "12345"}, tmp_path, "int")
    finally:
        _reset(mock_app)
    assert r.status == ReplayStatus.SUCCESS
    assert any(rec.condition_code == "MAINTENANCE_INTERSTITIAL" for rec in r.recoveries)
