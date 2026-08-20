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

from bankcua.escalation.handoff import (
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


def test_no_dead_wait_when_no_session_was_exposed(tmp_path):
    """Taking control requires attaching over CDP. If the run never exposed a
    session, no operator can resolve the request -- so blocking for the full
    timeout only delays the same failure. Abort at once, and say why."""
    import time as _t

    from bankcua.escalation.handoff import (HandoffCoordinator, HandoffStore,
                                            InterventionKind, InterventionRequest)

    store = HandoffStore(str(tmp_path / "handoffs"))
    coord = HandoffCoordinator(store, logger=None)
    coord.raise_intervention(InterventionRequest(
        id="replay-x-step9", kind=InterventionKind.RISKY_CONFIRMATION,
        reason="irreversible step needs confirmation", cdp_endpoint=None))

    started = _t.time()
    resolved = coord.wait_for_resolution("replay-x-step9", timeout_s=30.0)
    elapsed = _t.time() - started

    assert elapsed < 2.0, "should not have waited out the timeout"
    assert resolved.status.value == "aborted"
    assert "no live session" in resolved.resolution_note
    assert resolved.controller == "agent"
    # the request stays on disk for triage, exactly as a timeout would leave it
    assert store.read("replay-x-step9").status.value == "aborted"


def test_an_attachable_intervention_still_waits(tmp_path):
    """The fast path must not swallow the normal one."""
    import time as _t

    from bankcua.escalation.handoff import (HandoffCoordinator, HandoffStore,
                                            InterventionKind, InterventionRequest)

    store = HandoffStore(str(tmp_path / "handoffs"))
    coord = HandoffCoordinator(store, logger=None)
    coord.raise_intervention(InterventionRequest(
        id="replay-y-step9", kind=InterventionKind.RISKY_CONFIRMATION,
        reason="needs confirmation", cdp_endpoint="http://127.0.0.1:9222"))

    started = _t.time()
    resolved = coord.wait_for_resolution("replay-y-step9", timeout_s=2.0, poll_s=0.2)
    assert _t.time() - started >= 1.5      # it really waited
    assert resolved.status.value == "aborted"
    assert "timed out" in resolved.resolution_note


def test_wait_budget_is_configurable(tmp_path):
    """Two minutes suits an unattended run. A person who has to be fetched and
    briefed needs longer, and the run holds a live session open the whole time --
    so it is a knob, with the cost visible, rather than a constant."""
    import time as _t

    from bankcua.escalation.handoff import (HandoffCoordinator, HandoffStore,
                                            InterventionKind, InterventionRequest)

    store = HandoffStore(str(tmp_path / "h"))
    coord = HandoffCoordinator(store, logger=None, wait_timeout_s=1.0)
    coord.raise_intervention(InterventionRequest(
        id="r", kind=InterventionKind.RISKY_CONFIRMATION, reason="x",
        cdp_endpoint="http://127.0.0.1:9222"))

    started = _t.time()
    out = coord.wait_for_resolution("r", poll_s=0.1)
    assert 0.8 <= _t.time() - started < 3.0
    assert out.status.value == "aborted"
    assert "timed out after 1s" in out.resolution_note


def test_a_resolved_intervention_is_recorded_on_a_successful_run(mock_app, tmp_path):
    """"Who approved this?" is the first question asked about an irreversible
    action on a member's account.

    `intervention_id` previously appeared only when a run ended ESCALATED, so a
    run a supervisor had personally authorised came back looking exactly like one
    that ran unattended. The audit trail existed in the log and nowhere in the
    contract the caller keeps.
    """
    pytest.importorskip("playwright")
    import threading
    import time as _t

    from bankcua.escalation.handoff import HandoffCoordinator, InterventionStatus
    from bankcua.observability.logging import RunLogger
    from bankcua.replay.engine import ReplayEngine
    from bankcua.replay.result import ReplayStatus
    from bankcua.safety.policy import Policy, PolicyEngine
    from bankcua.schema import CapabilityArtifact
    from bankcua.surface.web_playwright import WebSurface

    art_path = os.path.join(ROOT, "capabilities", "corebank.open_subaccount.json")
    if not os.path.exists(art_path):
        pytest.skip("subaccount artifact missing")
    art = CapabilityArtifact.from_json(open(art_path).read())
    art.target.base_url = mock_app
    risky = next(s.index for s in art.steps if s.risk.value == "risky")
    req_id = f"replay-{art.id}-step{risky}"

    store = HandoffStore(str(tmp_path / "handoffs"))

    def operator():
        """Approve without performing the step, so replay itself completes it."""
        for _ in range(100):
            try:
                req = store.read(req_id)
                if req.status == InterventionStatus.OPEN:
                    req.status = InterventionStatus.RESOLVED
                    req.resume = True
                    req.controller = "agent"
                    req.resolution_note = "approved by test supervisor"
                    store.write(req)
                    return
            except Exception:
                pass
            _t.sleep(0.1)

    thread = threading.Thread(target=operator)
    thread.start()
    logger = RunLogger(str(tmp_path / "run"), "replay", art.secret_params(),
                       ["operator", "password123"])
    pe = PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"]),
                      allow_risky_override=False)
    surf = WebSurface(art.target.base_url, headless=True, cdp_port=_free_port())
    surf.start()
    try:
        res = ReplayEngine(surf, pe, logger,
                           HandoffCoordinator(store, logger, wait_timeout_s=25)
                           ).run(art, {"username": "operator",
                                       "password": "password123",
                                       "member_id": "12345",
                                       "acct_type": "Money Market",
                                       "deposit": "10.00"})
    finally:
        thread.join(timeout=5)
        surf.stop()

    assert res.status == ReplayStatus.SUCCESS
    assert res.intervention_id == req_id, (
        "a run a human authorised is indistinguishable from an unattended one")


def test_a_human_authorised_run_captures_its_outcome_too(mock_app, tmp_path):
    """The evidence for an approved irreversible action showed the question and
    not the answer.

    A gated run captures `confirm_stepNN.png` -- the screen the operator was
    asked to authorise -- and then, on success, captured nothing further. So the
    record of a hold a supervisor personally approved contained the confirmation
    prompt and no image of what happened next. "What did I approve?" was
    answerable; "what did it then do?" was not.
    """
    pytest.importorskip("playwright")
    import threading
    import time as _t

    from bankcua.escalation.handoff import HandoffCoordinator, InterventionStatus
    from bankcua.observability.logging import RunLogger
    from bankcua.replay.engine import ReplayEngine
    from bankcua.replay.result import ReplayStatus
    from bankcua.safety.policy import Policy, PolicyEngine
    from bankcua.schema import CapabilityArtifact
    from bankcua.surface.web_playwright import WebSurface

    art_path = os.path.join(ROOT, "capabilities", "corebank.open_subaccount.json")
    if not os.path.exists(art_path):
        pytest.skip("subaccount artifact missing")
    art = CapabilityArtifact.from_json(open(art_path).read())
    art.target.base_url = mock_app
    risky = next(s.index for s in art.steps if s.risk.value == "risky")
    store = HandoffStore(str(tmp_path / "handoffs"))
    req_id = f"replay-{art.id}-step{risky}"

    def approve():
        for _ in range(100):
            try:
                req = store.read(req_id)
                if req.status == InterventionStatus.OPEN:
                    req.status = InterventionStatus.RESOLVED
                    req.resume, req.controller = True, "agent"
                    store.write(req)
                    return
            except Exception:
                pass
            _t.sleep(0.1)

    thread = threading.Thread(target=approve)
    thread.start()
    run_dir = tmp_path / "run"
    logger = RunLogger(str(run_dir), "replay", art.secret_params(), [])
    surf = WebSurface(art.target.base_url, headless=True, cdp_port=_free_port())
    surf.start()
    try:
        res = ReplayEngine(
            surf, PolicyEngine(Policy(allowed_url_patterns=["http://127.0.0.1:*"])),
            logger, HandoffCoordinator(store, logger, wait_timeout_s=20)
        ).run(art, {"username": "operator", "password": "password123",
                    "member_id": "12345", "acct_type": "Money Market",
                    "deposit": "10.00"})
    finally:
        thread.join(timeout=5)
        surf.stop()

    assert res.status == ReplayStatus.SUCCESS
    shots = {p.name for p in run_dir.glob("*.png")}
    assert any(n.startswith("confirm_step") for n in shots), "no record of the question"
    assert "completed_after_intervention.png" in shots, (
        f"no record of the outcome; captured only {sorted(shots)}")
