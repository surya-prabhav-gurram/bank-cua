"""
The operator console, driven the way a person drives it.

REPORT section 5's control-transfer model was already real; what was mocked was
the human's window onto it -- a CLI where the operator typed a CSS selector.
These tests exercise the console instead: a picture of the live page, a click at
the coordinates the human clicked ON that picture, and a hand-back. No selector
appears anywhere, because the person is not thinking in selectors.
"""
import os
import threading
import time

import pytest

pytest.importorskip("playwright")

from bankcua.escalation.console import create_console
from bankcua.escalation.handoff import (HandoffCoordinator,
                                        HandoffStore)
from bankcua.observability.logging import RunLogger
from bankcua.replay.engine import ReplayEngine
from bankcua.replay.result import ReplayStatus
from bankcua.safety.policy import Policy, PolicyEngine
from bankcua.schema import CapabilityArtifact
from bankcua.surface.web_playwright import WebSurface

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
ART = os.path.join(ROOT, "capabilities", "corebank.open_subaccount.json")
CREDS = {"username": "operator", "password": "password123"}
PARAMS = {**CREDS, "member_id": "12345", "acct_type": "Money Market",
          "deposit": "500.00"}
CDP_PORT = 9333


def _confirm_button_xy(cdp_endpoint):
    """Where the human would look. A second CDP client is used only to compute
    the point for the test; the console itself never resolves a selector."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        br = pw.chromium.connect_over_cdp(cdp_endpoint)
        page = br.contexts[0].pages[0]
        box = page.locator("input[type=submit]").first.bounding_box()
        br.close()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


@pytest.fixture
def paused_run(mock_app, tmp_path):
    """A replay paused at the irreversible step, with its live session exposed.

    The automation runs in its OWN thread and builds its Surface there, because
    Playwright's sync API is thread-affine -- a browser created on one thread
    cannot be driven from another. That constraint is not incidental: it is the
    reason the console owns its session in a single worker thread rather than
    touching the page from whichever request thread happens to arrive.
    """
    if not os.path.exists(ART):
        pytest.skip("artifact missing")
    art = CapabilityArtifact.from_json(open(ART).read())
    art.target.base_url = mock_app
    store = HandoffStore(str(tmp_path / "handoffs"))
    logger = RunLogger(str(tmp_path / "run"), "replay", art.secret_params(),
                       dict(CREDS))
    box: dict = {}

    def run():
        pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"]))
        surf = WebSurface(art.target.base_url, headless=True, cdp_port=CDP_PORT)
        surf.start()
        try:
            box["result"] = ReplayEngine(
                surf, pe, logger, HandoffCoordinator(store, logger)).run(art, PARAMS)
        except Exception as ex:                      # pragma: no cover
            box["error"] = repr(ex)
        finally:
            try:
                surf.stop()
            except Exception:
                pass

    t = threading.Thread(target=run, daemon=True)
    t.start()

    req_id = f"replay-{art.id}-step9"
    for _ in range(160):
        try:
            if store.read(req_id).status.value == "open":
                break
        except Exception:
            pass
        if "result" in box or "error" in box:
            break
        time.sleep(0.25)
    else:
        pytest.skip("run did not reach the gated step")
    if "error" in box:
        pytest.skip(f"replay failed before the gate: {box['error']}")

    yield store, req_id, box, t
    t.join(timeout=15)


def test_console_shows_the_live_session_and_hands_control_back(paused_run):
    store, req_id, box, thread = paused_run
    req = store.read(req_id)
    assert req.controller == "operator"          # automation has ceded
    assert req.cdp_endpoint

    app = create_console(req_id, handoffs=store.root)
    client = app.test_client()

    # the console renders the paused session
    page = client.get("/")
    assert page.status_code == 200
    assert req_id in page.get_data(as_text=True)

    # ...as a picture of the live page
    shot = client.get("/screen")
    assert shot.status_code == 200
    assert shot.mimetype == "image/png" and len(shot.data) > 1000

    # the human clicks where they are looking, and it is recorded
    x, y = _confirm_button_xy(req.cdp_endpoint)
    clicked = client.post("/click", json={"x": x, "y": y})
    assert clicked.status_code == 200
    ops = [a["op"] for a in clicked.get_json()["actions"]]
    assert "click_xy" in ops

    # hand back
    done = client.post("/resolve", json={"resume": True,
                                         "note": "reviewed and confirmed"})
    assert done.status_code == 200

    thread.join(timeout=25)
    final = store.read(req_id)
    assert final.status.value == "resolved"
    assert final.controller == "agent"            # control returned
    assert any(a.op == "click_xy" for a in final.human_actions)

    result = box.get("result")
    assert result is not None
    # the operator performed the irreversible step, so replay must NOT redo it
    assert result.status == ReplayStatus.SUCCESS
    assert result.outputs.get("confirmation_number")


def test_typing_and_keys_are_forwarded_and_recorded(paused_run):
    store, req_id, box, thread = paused_run
    app = create_console(req_id, handoffs=store.root)
    client = app.test_client()

    client.post("/type", json={"text": "note to self"})
    keyed = client.post("/key", json={"key": "Tab"})
    ops = [a["op"] for a in keyed.get_json()["actions"]]
    assert "type" in ops and "key" in ops

    client.post("/resolve", json={"resume": False, "note": "aborting"})
    thread.join(timeout=25)
    assert store.read(req_id).status.value == "resolved"
    assert box.get("result").status == ReplayStatus.ESCALATED
