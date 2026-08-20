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


def test_console_refuses_clearly_when_no_session_was_exposed(tmp_path):
    """A run started without --cdp-port never offered its session to anyone.

    The operator meets that fact at the worst possible moment -- a bank screen is
    paused and waiting on them -- so it has to arrive as a sentence telling them
    what to do, not as a stack trace.
    """
    from bankcua.escalation.console import NotAttachable, create_console
    from bankcua.escalation.handoff import (InterventionKind,
                                            InterventionRequest)

    store = HandoffStore(str(tmp_path / "handoffs"))
    store.write(InterventionRequest(
        id="replay-x-step1", kind=InterventionKind.RISKY_CONFIRMATION,
        reason="irreversible step needs confirmation", cdp_endpoint=None))

    with pytest.raises(NotAttachable) as ex:
        create_console("replay-x-step1", handoffs=store.root)
    msg = str(ex.value)
    assert "--cdp-port" in msg          # names the cause
    assert "operator resolve" in msg    # and offers the way out


def test_console_stops_cleanly_once_control_is_handed_back(paused_run):
    """After resolve the worker is gone. A page that keeps polling would queue to
    a dead thread, wait out the timeout and 500 -- dozens of times. Closed
    endpoints answer immediately instead."""
    from bankcua.escalation.console import create_console

    store, req_id, _box, thread = paused_run
    app = create_console(req_id, handoffs=store.root)
    client = app.test_client()

    done = client.post("/resolve", json={"resume": False, "note": "aborting"})
    assert done.status_code == 200
    assert done.get_json()["closed"] is True        # tells the page to stop

    # everything after handback answers at once, with a status the page can act on
    assert client.get("/screen").status_code == 410
    assert client.post("/click", json={"x": 1, "y": 1}).status_code == 410
    assert client.post("/key", json={"key": "Tab"}).status_code == 410

    # reloading the console is the most natural thing an operator does next, and
    # it used to be the one route that answered with a stack trace.
    #
    # 200 rather than 410, deliberately: 410 Gone is CACHEABLE, and consoles are
    # served from a small set of ports. A browser that saw this page once at
    # :8090 served it from cache to the next escalation's console on that port
    # without contacting the new server -- which looks exactly like a broken
    # handoff and leaves no trace in any log.
    home = client.get("/")
    assert home.status_code == 200
    assert home.headers.get("Cache-Control") == "no-store"
    body = home.get_data(as_text=True)
    assert "returned to the agent" in body and "close this window" in body
    assert "/screen" not in body                 # nothing left to poll

    # and a second Resume click is not an error
    again = client.post("/resolve", json={"resume": False})
    assert again.status_code == 200 and again.get_json()["closed"] is True

    thread.join(timeout=25)


def test_click_coordinates_are_scaled_and_never_collapse_to_the_origin():
    """Two mapping bugs, both found by driving the console by hand.

    The picture is rarely displayed at natural size, so raw offsets miss. And
    swapping `img.src` directly blanks the element while the next frame loads --
    during which `naturalWidth` is 0, so a click in that gap scales to (0,0) and
    is sent to the corner of a live banking screen. Frames are preloaded and a
    click with no frame is dropped rather than guessed.
    """
    from bankcua.escalation import console
    js = console._PAGE
    assert "new Image()" in js and "next.onload" in js      # preloaded, not swapped
    assert "img.naturalWidth||nw" in js                     # cached fallback
    assert "if(!w||!h) return;" in js                       # never guess


def test_page_tells_the_operator_when_control_has_gone_back():
    """A dimmed page with disabled buttons reads as 'broken'. Say what happened."""
    from bankcua.escalation import console
    assert "id=\"banner\"" in console._PAGE
    assert "returned to the agent" in console._PAGE
    assert "getElementById('banner').style.display='block'" in console._PAGE


def test_origin_clicks_are_rejected_server_side(paused_run):
    """Defence in depth: even if a client sends (0,0), the session must not
    receive a click nobody aimed."""
    from bankcua.escalation.console import create_console

    store, req_id, _box, thread = paused_run
    client = create_console(req_id, handoffs=store.root).test_client()

    bad = client.post("/click", json={"x": 0, "y": 0})
    assert bad.status_code == 400
    assert "did not map" in bad.get_json()["error"]
    assert bad.get_json()["actions"] == []        # nothing recorded

    client.post("/resolve", json={"resume": False, "note": "done"})
    thread.join(timeout=25)


def _open_request(rid):
    from bankcua.escalation.handoff import InterventionKind, InterventionRequest
    return InterventionRequest(id=rid, kind=InterventionKind.RISKY_CONFIRMATION,
                               reason="test", cdp_endpoint="http://127.0.0.1:9222",
                               created_at=1000.0)


def test_console_pages_are_never_cacheable(tmp_path, monkeypatch):
    """A cached console page is indistinguishable from a broken handoff.

    The closed page used to answer 410, which HTTP explicitly allows a browser to
    cache. Consoles are served from a small set of ports, so a browser that saw
    that page once at :8090 kept serving it from cache to the NEXT escalation's
    console on the same port -- never contacting the new server at all. The
    operator pressed Take control, was told control had already been returned,
    and no log anywhere recorded a request, because none was made.
    """
    import bankcua.escalation.console as console_mod

    class _ClosedWorker:
        _closed = True

        def call(self, *a, **kw):
            raise console_mod.SessionClosed("control has been returned")

        def close(self): ...

    monkeypatch.setattr(console_mod, "_SessionWorker",
                        lambda store, rid: _ClosedWorker())
    store = HandoffStore(str(tmp_path))
    store.write(_open_request("cache-probe"))
    app = console_mod.create_console("cache-probe", handoffs=str(tmp_path))

    r = app.test_client().get("/")
    assert r.status_code == 200, (
        "410 Gone is cacheable; a browser will serve this page to the next "
        "console bound to the same port")
    assert r.headers.get("Cache-Control") == "no-store"
    assert b"Control has been returned" in r.data
