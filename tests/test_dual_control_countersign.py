"""
A dual-control pause is a DECISION, and the console has to ask for one.

The failure this file exists to prevent, found by driving the demo: a $2,000
transfer paused for dual control, and the dashboard offered "Take control of
this session". There was nothing to take control OF -- the value check runs
before the browser is sent anywhere -- so an operator got a blank page whose only
exit was to abort the run, and the result came back `escalated` with
`observed: resolved`, which reads like the system contradicting itself.

Two different things stop a run and they need different answers from a person:

  * a gated irreversible step needs someone to DRIVE the paused screen;
  * a dual-control threshold needs a second person to APPROVE.

Conflating them cost the second one its whole meaning, because "resolved" was
recorded without any record of WHO resolved it -- so the counter-signature was a
click, not an identity.
"""
import json
import os

import pytest

from bankcua.auth import (PrincipalStore, SessionAuthority, SessionSigner,
                          hash_password)
from bankcua.escalation.handoff import (HandoffCoordinator, HandoffStore,
                                        InterventionKind, InterventionRequest,
                                        InterventionStatus)

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
CATALOG = os.path.join(ROOT, "capabilities", "meridian")


def _pause(store, initiator="teller1", req_id="replay-x-dualcontrol"):
    """A dual-control pause as the engine raises one: no session, no screen."""
    req = InterventionRequest(
        id=req_id, kind=InterventionKind.DUAL_CONTROL,
        reason="dual control required: 'amount' exceeds 1000 USD",
        capability_id="meridian.transfer_funds", initiator=initiator,
        state_url="", cdp_endpoint=None, created_at=1000.0)
    store.write(req)
    return req


# ---------------------------------------------------------------------------
# The coordinator
# ---------------------------------------------------------------------------
def test_a_dual_control_pause_is_not_aborted_for_want_of_a_live_session(tmp_path):
    """The early abort exists because a gated STEP cannot be resolved without a
    CDP port -- blocking for the full timeout would delay a failure without
    changing it. Applied to a dual-control pause it means no large transfer
    could ever be approved, because that pause exposes no session by design."""
    store = HandoffStore(str(tmp_path))
    req = _pause(store)
    coordinator = HandoffCoordinator(store, None, wait_timeout_s=1.0)
    resolved = coordinator.wait_for_resolution(req.id, timeout_s=1.0)
    # Timed out waiting for a person -- NOT aborted as unattendable.
    assert "no live session was exposed" not in resolved.resolution_note


def test_a_gated_step_with_no_session_is_still_aborted_immediately(tmp_path):
    """The negative control: the original behaviour has to survive."""
    store = HandoffStore(str(tmp_path))
    store.write(InterventionRequest(
        id="replay-y-step10", kind=InterventionKind.RISKY_CONFIRMATION,
        reason="irreversible step", cdp_endpoint=None, created_at=1000.0))
    resolved = HandoffCoordinator(store, None, wait_timeout_s=30.0
                                  ).wait_for_resolution("replay-y-step10")
    assert resolved.status == InterventionStatus.ABORTED
    assert "no live session was exposed" in resolved.resolution_note


# ---------------------------------------------------------------------------
# The console
# ---------------------------------------------------------------------------
@pytest.fixture
def console(tmp_path):
    """The dashboard with a signed-in console in front of it."""
    from bankcua.dashboard import create_app
    principals = tmp_path / "principals.json"
    principals.write_text(json.dumps({
        "super1": {"kind": "staff", "role": "supervisor", "acts_as": "super1",
                   "password_hash": hash_password("password", rounds=1000)},
        "super2": {"kind": "staff", "role": "supervisor", "acts_as": "super2",
                   "password_hash": hash_password("password", rounds=1000)},
        "teller1": {"kind": "staff", "role": "teller", "acts_as": "teller1",
                    "password_hash": hash_password("password", rounds=1000)},
    }))
    authority = SessionAuthority(store=PrincipalStore(str(principals)),
                                 signer=SessionSigner(b"countersign-test"))
    handoffs = tmp_path / "handoffs"
    app = create_app(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path / "ev"),),
                     handoff_dir=str(handoffs), session_authority=authority,
                     require_session=True)
    return app.test_client(), HandoffStore(str(handoffs)), authority


def _as(authority, username):
    return {"X-Bankcua-Session": authority.mint(authority.store.get(username))}


def test_the_panel_asks_for_a_signature_not_for_control(console):
    client, store, authority = console
    _pause(store)
    row = client.get("/api/interventions",
                     headers=_as(authority, "super1")).get_json()[0]
    assert row["needs"] == "countersignature"
    # The bug in one assertion: offering control here hands an operator a blank
    # page whose only exit is aborting the run.
    assert row["attachable"] is False
    assert row["initiator"] == "teller1"


def test_a_supervisor_can_counter_sign_and_the_identity_is_recorded(console):
    client, store, authority = console
    req = _pause(store)
    body = client.post(f"/api/interventions/{req.id}/countersign",
                       headers=_as(authority, "super1")).get_json()
    assert body["resolved_by"] == "super1"
    saved = store.read(req.id)
    assert saved.status == InterventionStatus.RESOLVED
    assert saved.resume is True
    # The identity is the whole point: "resolved" alone is a click, not a
    # counter-signature, and the engine cannot check a click for independence.
    assert saved.resolved_by == "super1"
    assert saved.controller == "agent"


def test_the_person_who_started_the_run_cannot_approve_it(console):
    """Dual control, stated. Said plainly in the response because a button that
    fails with a policy error reads as a broken console rather than as the
    control working."""
    client, store, authority = console
    req = _pause(store, initiator="super1")
    r = client.post(f"/api/interventions/{req.id}/countersign",
                    headers=_as(authority, "super1"))
    assert r.status_code == 403
    assert "cannot approve itself" in r.get_json()["error"]
    assert store.read(req.id).status == InterventionStatus.OPEN


def test_a_teller_cannot_counter_sign(console):
    """A counter-signature must come from a supervisor. The teller is the case
    that matters: they can invoke the transfer that raised this pause, so
    without the role check dual control would be one person twice."""
    client, store, authority = console
    req = _pause(store)
    r = client.post(f"/api/interventions/{req.id}/countersign",
                    headers=_as(authority, "teller1"))
    assert r.status_code == 403
    assert store.read(req.id).status == InterventionStatus.OPEN


def test_a_counter_signature_is_never_taken_from_the_request_body(console):
    """An approver a caller can type is a string, not a second pair of eyes.
    The identity comes from the session or the request is refused."""
    client, store, _authority = console
    req = _pause(store)
    r = client.post(f"/api/interventions/{req.id}/countersign",
                    json={"approver": "super1"})
    assert r.status_code in (401, 302)
    assert store.read(req.id).status == InterventionStatus.OPEN


def test_taking_control_is_refused_on_a_pause_that_has_no_session(console):
    client, store, authority = console
    req = _pause(store)
    r = client.post(f"/api/interventions/{req.id}/console",
                    headers=_as(authority, "super1"))
    assert r.status_code == 409
    assert "counter-signature" in r.get_json()["error"]


def test_an_already_resolved_pause_cannot_be_signed_twice(console):
    client, store, authority = console
    req = _pause(store)
    assert client.post(f"/api/interventions/{req.id}/countersign",
                       headers=_as(authority, "super1")).status_code == 200
    again = client.post(f"/api/interventions/{req.id}/countersign",
                        headers=_as(authority, "super2"))
    assert again.status_code == 409
    assert store.read(req.id).resolved_by == "super1"


# ---------------------------------------------------------------------------
# The engine, which is where the decision actually belongs
# ---------------------------------------------------------------------------
class _FakeCoordinator:
    """Stands in for a person: records what was raised, answers with a fixed
    resolution."""

    def __init__(self, resolution):
        self.resolution = resolution
        self.raised = None

    def raise_intervention(self, req):
        self.raised = req
        self.resolution.id = req.id
        return req

    def wait_for_resolution(self, req_id, **kw):
        return self.resolution


def _engine(coordinator, initiator="teller1"):
    from bankcua.replay.engine import ReplayEngine
    from bankcua.safety.policy import Policy, PolicyEngine

    class _Logger:
        def __init__(self): self.events = []
        def event(self, name, **kw): self.events.append((name, kw))

    policy = PolicyEngine(Policy(approvers={"super1", "super2"},
                                 strict_approvers=True))
    return ReplayEngine(None, policy, _Logger(), coordinator,
                        initiator=initiator)


def _artifact():
    return type("A", (), {"id": "meridian.transfer_funds", "version": "1.0.0"})()


def _decision():
    return type("V", (), {"reason": "'amount' exceeds 1000 USD",
                          "params": {"amount": "2000.00"}})()


def _resolved(resolved_by, resume=True,
              status=InterventionStatus.RESOLVED):
    return InterventionRequest(id="x", kind=InterventionKind.DUAL_CONTROL,
                               reason="r", status=status, resume=resume,
                               resolved_by=resolved_by)


def test_the_engine_proceeds_only_on_an_independent_signature():
    coordinator = _FakeCoordinator(_resolved("super1"))
    assert _engine(coordinator)._satisfy_dual_control(_artifact(),
                                                      _decision()) is None
    # The pause carries the initiator across the handoff, because independence
    # is only checkable relative to it.
    assert coordinator.raised.initiator == "teller1"
    # ...and it advertises no session, because there is nothing to drive.
    assert coordinator.raised.cdp_endpoint is None


def test_a_console_saying_resolved_does_not_by_itself_authorise_the_run():
    """A resolution posted by any console is a claim that somebody clicked. It
    is not a ruling on whether that somebody could counter-sign THIS run, so the
    engine re-checks rather than trusting the resolver."""
    engine = _engine(_FakeCoordinator(_resolved("teller1")), initiator="teller1")
    res = engine._satisfy_dual_control(_artifact(), _decision())
    assert res is not None and res.status.value == "escalated"
    assert res.failure.code == "DUAL_CONTROL_REQUIRED"
    assert "cannot counter-sign" in res.failure.observed


def test_a_resolution_with_no_recorded_approver_is_not_a_counter_signature():
    """The original bug: the run resumed on `resolved` alone, with no record of
    who. That is a click, and a click cannot be checked for independence."""
    engine = _engine(_FakeCoordinator(_resolved("")))
    res = engine._satisfy_dual_control(_artifact(), _decision())
    assert res is not None and res.status.value == "escalated"
    assert "without recording who approved it" in res.failure.observed


def test_an_unregistered_approver_cannot_counter_sign():
    """Strict mode: independence from the initiator is necessary, not
    sufficient. `--approver whoever` is theatre without a registry."""
    engine = _engine(_FakeCoordinator(_resolved("someone-else")))
    res = engine._satisfy_dual_control(_artifact(), _decision())
    assert res is not None and res.status.value == "escalated"


def test_nobody_answering_reads_as_nobody_answering():
    """The message a reviewer sees. `expected a second reviewer to counter-sign
    / observed resolved` was the old text, and it reads like the system
    contradicting itself."""
    engine = _engine(_FakeCoordinator(
        _resolved("", resume=False, status=InterventionStatus.ABORTED)))
    res = engine._satisfy_dual_control(_artifact(), _decision())
    assert "nobody counter-signed" in res.failure.observed
