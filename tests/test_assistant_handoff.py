"""
A pause is answered where the person who caused it is sitting.

Someone who asks the chatbot to move money is watching the chat. Before this, the
run stopped for a confirmation and the question -- "may I post this?" -- appeared
on the dashboard, a different tab, while their own request sat on an open
connection looking like it had hung. The answer was reachable; it just was not
anywhere they were looking.

So an intervention now records WHICH SURFACE raised it, the assistant polls for
its own while a call is in flight, and the operator queue stops listing the ones
that are already in front of somebody.

The second half of that is what these tests are mostly about, because it is
where it could go wrong. Hiding a pause from the dashboard is only safe when the
person it was hidden FOR can actually clear it. A teller's confirmation needs a
supervisor; a dual-control pause needs a second person by definition. Hide
either and the run waits in a window whose occupant cannot answer it, then times
out -- which is worse than showing it twice.
"""
import json
import os
import time

import pytest

from bankcua.auth import (PrincipalStore, SessionAuthority, SessionSigner,
                          hash_password)
from bankcua.dashboard import _answered_in_the_assistant
from bankcua.dashboard import create_app as create_dashboard
from bankcua.escalation.handoff import (HandoffStore, InterventionKind,
                                        InterventionRequest)
from bankcua.safety.credentials import OperatorIdentity, StaticCredentialStore
from bankcua.service import create_app as create_api

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
CATALOG = os.path.join(ROOT, "capabilities", "meridian")

CREDS = StaticCredentialStore({
    "teller1": OperatorIdentity("teller1", "teller", {"password": "pw"}, {}),
    "super1": OperatorIdentity("super1", "supervisor", {"password": "pw"}, {}),
})


@pytest.fixture
def authority(tmp_path):
    path = tmp_path / "principals.json"
    path.write_text(json.dumps({
        "super1": {"kind": "staff", "role": "supervisor", "acts_as": "super1",
                   "password_hash": hash_password("password", rounds=1000)},
        "super2": {"kind": "staff", "role": "supervisor", "acts_as": "super1",
                   "password_hash": hash_password("password", rounds=1000)},
        "teller1": {"kind": "staff", "role": "teller", "acts_as": "teller1",
                    "password_hash": hash_password("password", rounds=1000)},
        "100234": {"kind": "member", "role": "member", "member_id": "100234",
                   "password_hash": hash_password("member123", rounds=1000)},
    }))
    return SessionAuthority(store=PrincipalStore(str(path)),
                            signer=SessionSigner(b"test-key"))


def _as(authority, username):
    return {"X-Bankcua-Session": authority.mint(authority.store.get(username))}


def _raise(handoffs, *, ident="replay-x-step9", channel="assistant",
           role="supervisor", kind=InterventionKind.RISKY_CONFIRMATION,
           initiator="super1"):
    req = InterventionRequest(
        id=ident, kind=kind,
        reason="irreversible step 'Post the transfer' needs human confirmation",
        capability_id="meridian.transfer_funds", current_step_index=13,
        initiator=initiator, channel=channel, initiator_role=role,
        created_at=time.time())
    HandoffStore(str(handoffs)).write(req)
    return req


# ---------------------------------------------------------------------------
# What the operator queue stops showing, and what it must keep showing
# ---------------------------------------------------------------------------
def test_a_supervisors_own_chat_pause_leaves_the_operator_queue(tmp_path,
                                                                authority):
    """It is already in front of them, in the window they are looking at.

    Listing it here as well puts one irreversible step in front of two people,
    and the one who did not ask for it has no context for what they would be
    approving.
    """
    handoffs = tmp_path / "handoffs"
    _raise(handoffs)
    app = create_dashboard(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),),
                           handoff_dir=str(handoffs), session_authority=authority)
    assert app.test_client().get("/api/interventions").get_json() == []


def test_a_tellers_chat_pause_stays_in_the_queue(tmp_path, authority):
    """A teller cannot confirm an irreversible step -- a supervisor must.

    Hiding this one would leave it waiting in a window whose occupant is not
    allowed to answer it, until it times out. The person who needs to see it is
    precisely the one at the operator queue.
    """
    handoffs = tmp_path / "handoffs"
    _raise(handoffs, role="teller", initiator="teller1")
    app = create_dashboard(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),),
                           handoff_dir=str(handoffs), session_authority=authority)
    shown = app.test_client().get("/api/interventions").get_json()
    assert [r["id"] for r in shown] == ["replay-x-step9"]


def test_a_dual_control_pause_from_chat_stays_in_the_queue(tmp_path, authority):
    """Dual control asks for a SECOND person, so it can never be answered by
    the one who raised it. It belongs in the queue by definition."""
    handoffs = tmp_path / "handoffs"
    _raise(handoffs, kind=InterventionKind.DUAL_CONTROL)
    app = create_dashboard(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),),
                           handoff_dir=str(handoffs), session_authority=authority)
    shown = app.test_client().get("/api/interventions").get_json()
    assert [r["needs"] for r in shown] == ["countersignature"]


def test_a_dashboard_pause_is_untouched(tmp_path, authority):
    """The rule is about WHERE it was raised, not about hiding pauses."""
    handoffs = tmp_path / "handoffs"
    _raise(handoffs, channel="dashboard")
    app = create_dashboard(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),),
                           handoff_dir=str(handoffs), session_authority=authority)
    assert len(app.test_client().get("/api/interventions").get_json()) == 1


def test_an_agents_pause_over_the_api_is_untouched(tmp_path, authority):
    """No channel at all: an unattended caller has no window to show it in, so
    the operator queue is the only place it can appear."""
    handoffs = tmp_path / "handoffs"
    _raise(handoffs, channel="", role="")
    app = create_dashboard(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),),
                           handoff_dir=str(handoffs), session_authority=authority)
    assert len(app.test_client().get("/api/interventions").get_json()) == 1


def test_the_hiding_rule_states_both_halves():
    """Read directly, because the rule is the load-bearing part.

    Both conditions have to hold: raised in the assistant, AND of a kind the
    person who raised it may clear. Either alone is not enough.
    """
    class R:
        def __init__(self, channel, kind, role):
            self.channel, self.initiator_role = channel, role
            self.kind = type("K", (), {"value": kind})()

    assert _answered_in_the_assistant(
        R("assistant", "risky_confirmation", "supervisor"))
    assert not _answered_in_the_assistant(
        R("assistant", "risky_confirmation", "teller"))
    assert not _answered_in_the_assistant(
        R("assistant", "dual_control", "supervisor"))
    assert not _answered_in_the_assistant(
        R("dashboard", "risky_confirmation", "supervisor"))


# ---------------------------------------------------------------------------
# The API endpoints the assistant is built on
# ---------------------------------------------------------------------------
@pytest.fixture
def api(tmp_path, authority):
    cfg = tmp_path / "service.yaml"
    cfg.write_text(json.dumps({"default_operator": "teller1"}))
    app = create_api(catalog_dir=CATALOG, service_config_path=str(cfg),
                     evidence_dir=str(tmp_path / "ev"), credential_store=CREDS,
                     handoff_dir=str(tmp_path / "handoffs"),
                     session_authority=authority)
    return app.test_client()


def test_the_api_lists_open_pauses_with_the_channel_that_raised_them(
        api, tmp_path, authority):
    _raise(tmp_path / "handoffs")
    rows = api.get("/interventions", headers=_as(authority, "super1")).get_json()
    assert [(r["id"], r["channel"], r["needs"]) for r in rows] == [
        ("replay-x-step9", "assistant", "confirmation")]


def test_a_member_is_never_shown_a_paused_run(api, tmp_path, authority):
    """A paused run describes whichever member an operator was working on."""
    _raise(tmp_path / "handoffs")
    assert api.get("/interventions",
                   headers=_as(authority, "100234")).get_json() == []


def test_confirming_requires_a_signed_in_supervisor(api, tmp_path, authority):
    _raise(tmp_path / "handoffs")
    anon = api.post("/interventions/replay-x-step9/confirm")
    assert anon.status_code == 401
    assert anon.get_json()["refusal"]["code"] == "SESSION_REQUIRED"

    teller = api.post("/interventions/replay-x-step9/confirm",
                      headers=_as(authority, "teller1"))
    assert teller.status_code == 403
    assert teller.get_json()["refusal"]["code"] == \
        "CONFIRMATION_NOT_PERMITTED_FOR_ROLE"


def test_a_dual_control_pause_cannot_be_cleared_by_confirming(api, tmp_path,
                                                              authority):
    """Two signatures must not collapse into one.

    Confirming is one supervisor saying "post it as it stands". Dual control is
    the institution requiring a second, independent person. Letting the same
    button answer both would quietly remove the second one.
    """
    _raise(tmp_path / "handoffs", kind=InterventionKind.DUAL_CONTROL)
    r = api.post("/interventions/replay-x-step9/confirm",
                 headers=_as(authority, "super1"))
    assert r.status_code == 409
    assert "counter-signature" in r.get_json()["error"]


def test_confirming_records_who_approved_and_hands_control_back(
        api, tmp_path, authority):
    _raise(tmp_path / "handoffs")
    r = api.post("/interventions/replay-x-step9/confirm",
                 headers=_as(authority, "super1"))
    assert r.status_code == 200 and r.get_json()["resolved_by"] == "super1"

    stored = HandoffStore(str(tmp_path / "handoffs")).read("replay-x-step9")
    assert stored.status.value == "resolved"
    assert stored.resolved_by == "super1"
    assert stored.controller == "agent" and stored.resume is True
    # The engine reads this note to decide whether a human already performed the
    # step. Nobody touched the page, so it must NOT say "manual" -- otherwise
    # replay skips the step it was authorised to perform.
    assert "manual" not in stored.resolution_note.lower()
    assert stored.human_actions == []


def test_a_pause_already_answered_is_not_answered_twice(api, tmp_path,
                                                        authority):
    _raise(tmp_path / "handoffs")
    first = api.post("/interventions/replay-x-step9/confirm",
                     headers=_as(authority, "super1"))
    second = api.post("/interventions/replay-x-step9/confirm",
                      headers=_as(authority, "super2"))
    assert first.status_code == 200
    assert second.status_code == 409 and "already" in second.get_json()["error"]


def test_an_intervention_id_cannot_climb_out_of_the_store(api, authority):
    """Ids are matched with `<path:...>` because they are long and dotted, and
    the store joins one straight onto a directory."""
    for probe in ("..%2f..%2fetc%2fpasswd", "../../config/session.key"):
        r = api.post(f"/interventions/{probe}/confirm",
                     headers=_as(authority, "super1"))
        assert r.status_code == 404


def test_the_channel_a_caller_sends_cannot_be_anything_it_likes(tmp_path,
                                                                authority):
    """`channel` decides which surface polls for the pause, so an unknown one
    would raise an intervention that no window is watching for."""
    from bankcua.service import create_app
    cfg = tmp_path / "service.yaml"
    cfg.write_text(json.dumps({"default_operator": "teller1"}))
    app = create_app(catalog_dir=CATALOG, service_config_path=str(cfg),
                     evidence_dir=str(tmp_path / "ev"), credential_store=CREDS,
                     handoff_dir=str(tmp_path / "handoffs"),
                     session_authority=authority)
    # Reaches the guard without launching anything: an unknown capability is
    # refused first, so what is asserted here is the parsing, not a run.
    r = app.test_client().post("/invoke/nope",
                               json={"params": {}, "channel": "../evil"})
    assert r.status_code == 404
    import inspect

    import bankcua.service as svc
    source = inspect.getsource(svc.create_app)
    assert 'if channel not in ("assistant", "dashboard", "")' in source
