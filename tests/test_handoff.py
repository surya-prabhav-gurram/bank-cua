"""
Escalation & handoff. Unit tests for the file-based inbox and control token,
plus a live integration test of the full control transfer over CDP: replay
pauses at the irreversible step, a human operator attaches to the SAME browser,
performs the confirm, and hands control back; replay resumes to success.
"""
import os
import socket
import threading
import time

import pytest

from bankcua.escalation.handoff import (                # noqa: E402
    HandoffStore, HandoffCoordinator, InterventionRequest, InterventionKind,
    InterventionStatus)

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")


def _req(rid="r1", **kw):
    return InterventionRequest(id=rid, kind=InterventionKind.RISKY_CONFIRMATION,
                               reason="needs a human", **kw)


# ---- unit: store + coordinator (no browser) ---------------------------------
def test_store_roundtrip_and_list_open(tmp_path):
    store = HandoffStore(str(tmp_path))
    store.write(_req("open1"))
    resolved = _req("done1")
    resolved.status = InterventionStatus.RESOLVED
    store.write(resolved)
    got = store.read("open1")
    assert got.id == "open1" and got.status == InterventionStatus.OPEN
    open_ids = [r.id for r in store.list_open()]
    assert open_ids == ["open1"]              # resolved one excluded


def test_coordinator_cedes_control_on_raise(tmp_path):
    coord = HandoffCoordinator(HandoffStore(str(tmp_path)))
    r = coord.raise_intervention(_req("x"))
    assert r.controller == "operator"          # control ceded immediately
    assert r.status == InterventionStatus.OPEN


def test_coordinator_wait_returns_on_resolution(tmp_path):
    store = HandoffStore(str(tmp_path))
    coord = HandoffCoordinator(store)
    coord.raise_intervention(_req("y"))
    # operator resolves (control back to agent)
    r = store.read("y")
    r.status = InterventionStatus.RESOLVED
    r.controller = "agent"
    store.write(r)
    out = coord.wait_for_resolution("y", timeout_s=2, poll_s=0.1)
    assert out.status == InterventionStatus.RESOLVED
    assert out.controller == "agent"


def test_coordinator_wait_times_out_and_aborts(tmp_path):
    store = HandoffStore(str(tmp_path))
    coord = HandoffCoordinator(store)
    coord.raise_intervention(_req("z"))
    out = coord.wait_for_resolution("z", timeout_s=0.4, poll_s=0.1)  # nobody home
    assert out.status == InterventionStatus.ABORTED
    assert out.controller == "agent"           # control returns even on abort


# ---- integration: full live-session control transfer over CDP ---------------
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_live_handoff_over_cdp(mock_app, tmp_path):
    pytest.importorskip("playwright")
    from bankcua.schema import CapabilityArtifact
    from bankcua.replay.engine import ReplayEngine
    from bankcua.replay.result import ReplayStatus
    from bankcua.observability.logging import RunLogger
    from bankcua.safety.policy import Policy, PolicyEngine
    from bankcua.surface.web_playwright import WebSurface
    from bankcua.escalation.handoff import OperatorSession

    art_path = os.path.join(ROOT, "capabilities", "corebank.open_subaccount.json")
    if not os.path.exists(art_path):
        pytest.skip("subaccount artifact missing")
    art = CapabilityArtifact.from_json(open(art_path).read())
    art.target.base_url = mock_app
    risky = next(s.index for s in art.steps if s.risk.value == "risky")
    req_id = f"replay-{art.id}-step{risky}"

    store = HandoffStore(str(tmp_path / "handoffs"))
    logger = RunLogger(str(tmp_path / "run"), "replay", art.secret_params(),
                       ["operator", "password123"])
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"]),
                      allow_risky_override=False)          # forces the gate
    surf = WebSurface(art.target.base_url, headless=True, cdp_port=_free_port())
    surf.start()
    coord = HandoffCoordinator(store, logger)

    def operator():
        for _ in range(200):
            try:
                r = store.read(req_id)
                if r.status.value == "open" and r.cdp_endpoint:
                    break
            except Exception:
                pass
            time.sleep(0.1)
        else:
            return
        sess = OperatorSession(store.read(req_id), store).attach()
        try:
            sess.do("note", "reviewed; approving")
            sess.do("click_selector", "input[type=submit]")   # Confirm and Create
            time.sleep(0.3)
            sess.resolve(note="performed manually by operator", resume=True)
        finally:
            sess.detach()

    t = threading.Thread(target=operator)
    t.start()
    try:
        res = ReplayEngine(surf, pe, logger, coord).run(
            art, {"username": "operator", "password": "password123",
                  "member_id": "12345", "acct_type": "Money Market",
                  "deposit": "500.00"})
    finally:
        t.join(timeout=8)
        surf.stop()

    assert res.status == ReplayStatus.SUCCESS
    assert res.outputs.get("confirmation_number", "").startswith("SA-")
    fin = store.read(req_id)
    assert fin.status == InterventionStatus.RESOLVED
    assert fin.controller == "agent"                       # control handed back
    assert len(fin.human_actions) >= 2                     # note + click recorded
