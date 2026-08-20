"""
The `Surface` seam, demonstrated rather than argued.

REPORT section 4 claims a new surface is "a new Surface implementation and
touches nothing else". These tests are that claim, executable: the SAME artifact,
recorded on the Playwright/DOM surface, is replayed through a surface that has no
DOM at all -- it perceives an accessibility tree and acts with a mouse and a
keyboard, the way a Windows UIA or macOS AX driver does.

Nothing about the schema, the compiler, the replay engine, the error taxonomy or
the safety model changes between the two runs.
"""
import os

import pytest

pytest.importorskip("playwright")

from bankcua.portability import portability_report
from bankcua.replay.engine import ReplayEngine
from bankcua.replay.result import ReplayStatus
from bankcua.observability.logging import RunLogger
from bankcua.safety.policy import Policy, PolicyEngine
from bankcua.schema import (CapabilityArtifact, Locator,
                            LocatorCandidate, LocatorKind)
from bankcua.surface.accessibility import AccessibilitySurface
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


def _replay(art, params, tmp_path, name, surface):
    logger = RunLogger(str(tmp_path / name), "replay", art.secret_params(),
                       {n: str(params[n]) for n in art.secret_params()})
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"]))
    surface.start()
    try:
        return ReplayEngine(surface, pe, logger, None).run(art, params)
    finally:
        surface.stop()


# ---- the headline ---------------------------------------------------------
def test_web_recorded_artifact_replays_on_a_surface_with_no_dom(mock_app, tmp_path):
    art = _load(mock_app)
    res = _replay(art, {**CREDS, "member_id": "12345"}, tmp_path, "ax",
                  AccessibilitySurface(art.target.base_url, headless=True))

    assert res.status == ReplayStatus.SUCCESS
    assert res.outputs == {"member_name": "Jane A. Doe", "savings_balance": 421355}


def test_both_surfaces_produce_identical_outputs(mock_app, tmp_path):
    """Same contract, same answers -- the surface is an implementation detail."""
    art = _load(mock_app)
    params = {**CREDS, "member_id": "12345"}
    web = _replay(art, params, tmp_path, "w",
                  WebSurface(art.target.base_url, headless=True))
    ax = _replay(art, params, tmp_path, "a",
                 AccessibilitySurface(art.target.base_url, headless=True))
    assert web.status == ax.status == ReplayStatus.SUCCESS
    assert web.outputs == ax.outputs


def test_business_outcomes_are_detected_on_the_second_surface_too(mock_app, tmp_path):
    """The error taxonomy is surface-agnostic: it reads text, and an a11y tree has
    text. `surfaces_outputs` works here as well."""
    art = _load(mock_app)
    res = _replay(art, {**CREDS, "member_id": "99999"}, tmp_path, "denied",
                  AccessibilitySurface(art.target.base_url, headless=True))
    assert res.status == ReplayStatus.BUSINESS_OUTCOME
    assert res.business_outcome.code == "PERMISSION_DENIED"
    assert res.outputs.get("member_name") == "Restricted Account"


def test_not_found_is_also_a_business_outcome_here(mock_app, tmp_path):
    art = _load(mock_app)
    res = _replay(art, {**CREDS, "member_id": "00000"}, tmp_path, "missing",
                  AccessibilitySurface(art.target.base_url, headless=True))
    assert res.status == ReplayStatus.BUSINESS_OUTCOME
    assert res.business_outcome.code == "MEMBER_NOT_FOUND"


# ---- what the surface can and cannot honour ------------------------------
def test_the_surface_declares_it_has_no_dom():
    supported = AccessibilitySurface.supported_locator_kinds
    for dom_only in (LocatorKind.CSS, LocatorKind.XPATH, LocatorKind.TEST_ID):
        assert dom_only not in supported
    for portable in (LocatorKind.ROLE, LocatorKind.NEAR_LABEL, LocatorKind.COORDINATES):
        assert portable in supported


def test_dom_only_candidates_are_skipped_not_failed(mock_app, tmp_path):
    """An artifact recorded on the web carries CSS/XPath that is meaningless
    here. Skipping them -- rather than failing -- is what lets ONE recording
    serve both surfaces."""
    surface = AccessibilitySurface(mock_app, headless=True)
    surface.start()
    try:
        surface.navigate("/login")
        loc = Locator(description="user id", candidates=[
            LocatorCandidate(kind=LocatorKind.CSS, value="#nope"),
            LocatorCandidate(kind=LocatorKind.XPATH, value="//nope"),
            LocatorCandidate(kind=LocatorKind.NEAR_LABEL, value="User ID"),
        ])
        node, index = surface._resolve(loc)
        assert node is not None
        assert index == 2          # the two DOM strategies were skipped
    finally:
        surface.stop()


def test_frames_are_walked_because_an_ax_tree_stops_at_the_boundary(mock_app, tmp_path):
    """The balance lives in an iframe. An accessibility tree does not cross that
    boundary on its own -- the desktop analogue is a nested pane -- so the driver
    walks the frame tree explicitly."""
    art = _load(mock_app)
    res = _replay(art, {**CREDS, "member_id": "12345"}, tmp_path, "frames",
                  AccessibilitySurface(art.target.base_url, headless=True))
    assert res.outputs["savings_balance"] == 421355


# ---- portability is decidable before launching ---------------------------
def test_portability_is_answered_statically():
    art = CapabilityArtifact.from_json(open(ART).read())
    for cls, name in ((WebSurface, "web"), (AccessibilitySurface, "a11y")):
        report = portability_report(art, cls, name)
        assert report.portable, report.summary()
        assert report.blockers() == []


def test_a_dom_only_artifact_is_reported_unportable_before_it_runs():
    """Finding out that step 7 is unreachable AFTER steps 1-6 have run is how
    automation half-completes an irreversible flow. Ask first."""
    art = CapabilityArtifact.from_json(open(ART).read())
    for st in art.steps:
        if st.target:
            st.target.candidates = [c for c in st.target.candidates
                                    if c.kind in (LocatorKind.CSS, LocatorKind.XPATH)]
    report = portability_report(art, AccessibilitySurface, "a11y")
    assert report.portable is False
    assert report.blockers()
    assert "NOT portable" in report.summary()
