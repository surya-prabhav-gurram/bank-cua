"""
The artifact is a contract: a run may not report SUCCESS while a declared
output is missing.

A failed extract is deliberately NOT fatal at the step -- the condition
detectors still get their chance to explain *why* the value was absent. The
contract is enforced once, at the end of the run, as OUTPUT_EXTRACTION_FAILED.
"""
import os

import pytest

pytest.importorskip("playwright")

from bankcua.schema import (CapabilityArtifact, Locator, LocatorCandidate,
                            LocatorKind)
from bankcua.replay.engine import ReplayEngine
from bankcua.replay.result import ReplayStatus
from bankcua.observability.logging import RunLogger
from bankcua.safety.policy import Policy, PolicyEngine
from bankcua.surface.web_playwright import WebSurface

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
ART = os.path.join(ROOT, "capabilities", "corebank.member_savings_lookup.json")
CREDS = {"username": "operator", "password": "password123"}


def _load(mock_app):
    if not os.path.exists(ART):
        pytest.skip("artifact missing")
    a = CapabilityArtifact.from_json(open(ART).read())
    a.target.base_url = mock_app
    return a


def _replay(art, params, tmp_path, name):
    logger = RunLogger(str(tmp_path / name), "replay", art.secret_params(),
                       {n: str(params[n]) for n in art.secret_params()})
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"]))
    surf = WebSurface(art.target.base_url, headless=True)
    surf.start()
    try:
        return ReplayEngine(surf, pe, logger, None).run(art, params)
    finally:
        surf.stop()


def test_happy_path_populates_every_declared_output(mock_app, tmp_path):
    art = _load(mock_app)
    r = _replay(art, {**CREDS, "member_id": "12345"}, tmp_path, "ok")
    assert r.status == ReplayStatus.SUCCESS
    assert set(art.output_names()) <= set(r.outputs)


def test_unresolvable_extract_fails_the_contract(mock_app, tmp_path):
    """Break the balance extractor: the flow still reaches the success
    checkpoint, so without the contract check this would report SUCCESS with a
    silently missing output."""
    art = _load(mock_app)
    broken = next(s for s in art.steps
                  if s.extract and s.extract.output == "savings_balance")
    broken.extract.locator = Locator(
        description="deliberately unresolvable",
        frame_path=broken.extract.locator.frame_path,
        candidates=[LocatorCandidate(kind=LocatorKind.CSS,
                                     value="#no-such-element-anywhere",
                                     reasoning="test: resolves to nothing")])

    r = _replay(art, {**CREDS, "member_id": "12345"}, tmp_path, "broken")

    assert r.status == ReplayStatus.FAILURE
    assert r.failure.code == "OUTPUT_EXTRACTION_FAILED"
    assert "savings_balance" in r.failure.expected
    # the failure points at the step that should have produced the value
    assert r.failure.step_index == broken.index
    # the output that DID resolve is still surfaced for debugging
    assert r.outputs.get("member_name")
    # and the failure carries evidence
    assert r.failure.evidence.get("screenshot")
