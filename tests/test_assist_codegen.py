"""
Assisted-recovery mechanism and code generation.

The assist test breaks a step's locator so it cannot resolve, then supplies a
fake single-step recovery decision and asserts the run recovers, records the
assist, and stays within its bound.
"""
import ast
import os

import pytest

pytest.importorskip("playwright")

from bankcua.schema import CapabilityArtifact, LocatorCandidate, LocatorKind
from bankcua.replay.engine import ReplayEngine
from bankcua.replay.result import ReplayStatus
from bankcua.observability.logging import RunLogger
from bankcua.safety.policy import Policy, PolicyEngine
from bankcua.surface.web_playwright import WebSurface
from bankcua.agent.actions import DiscoveryAction
from bankcua.codegen import generate_playwright_script

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
ART = os.path.join(ROOT, "capabilities", "corebank.member_savings_lookup.json")
CREDS = {"username": "operator", "password": "password123"}


class _FakeAssist:
    """Stand-in LLM: on the failing Search step, clicks the search button (ref 1
    on the /home page: textbox=0, button=1)."""
    name = "fake"

    def decide(self, ctx):
        return DiscoveryAction(action="click", ref=1, intent="click the search button")


def test_assisted_recovery(mock_app, tmp_path):
    if not os.path.exists(ART):
        pytest.skip("artifact missing; run scripts/run_discovery.sh")
    art = CapabilityArtifact.from_json(open(ART).read())
    art.target.base_url = mock_app
    # break the Search click (step 4) so every recorded candidate fails
    art.steps[4].target.candidates = [
        LocatorCandidate(kind=LocatorKind.CSS, value="button#does-not-exist")]
    pol = Policy(allowed_url_patterns=["http://127.0.0.1:*"])
    pe = PolicyEngine(pol)
    logger = RunLogger(str(tmp_path / "assist"), "replay", art.secret_params(),
                       [CREDS["password"]])
    surf = WebSurface(art.target.base_url, headless=True)
    surf.start()
    try:
        res = ReplayEngine(surf, pe, logger, None,
                           assist_provider=_FakeAssist(), max_assists=1).run(
            art, {**CREDS, "member_id": "12345"})
    finally:
        surf.stop()
    assert res.status == ReplayStatus.SUCCESS
    assert len(res.assists) == 1 and res.assists[0].succeeded
    assert res.assists[0].step_index == 4


def test_assist_is_bounded(mock_app, tmp_path):
    """With max_assists=0, a broken step must hard-fail (no unbounded retry)."""
    if not os.path.exists(ART):
        pytest.skip("artifact missing")
    art = CapabilityArtifact.from_json(open(ART).read())
    art.target.base_url = mock_app
    art.steps[4].target.candidates = [
        LocatorCandidate(kind=LocatorKind.CSS, value="button#nope")]
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"]))
    logger = RunLogger(str(tmp_path / "noassist"), "replay", art.secret_params(),
                       [CREDS["password"]])
    surf = WebSurface(art.target.base_url, headless=True)
    surf.start()
    try:
        res = ReplayEngine(surf, pe, logger, None,
                           assist_provider=_FakeAssist(), max_assists=0).run(
            art, {**CREDS, "member_id": "12345"})
    finally:
        surf.stop()
    assert res.status == ReplayStatus.FAILURE


def test_codegen_produces_valid_python():
    if not os.path.exists(ART):
        pytest.skip("artifact missing")
    art = CapabilityArtifact.from_json(open(ART).read())
    code = generate_playwright_script(art)
    ast.parse(code)                       # must be syntactically valid
    assert "def run(" in code
    assert "get_by_role" in code or "locator(" in code
