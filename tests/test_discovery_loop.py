"""
Discovery loop + transcript->artifact compiler, driven by a scripted provider
(no live LLM) against the live mock. Covers the happy path plus the loop's
stopping/branch conditions: escalate, wall-clock timeout, stuck/no-progress,
finish-rejected, and policy block.
"""
import os

import pytest

pytest.importorskip("playwright")

from bankcua.agent.actions import DiscoveryAction
from bankcua.agent.loop import DiscoveryLoop
from bankcua.agent.task import DiscoveryTask
from bankcua.agent.compiler import compile_artifact
from bankcua.escalation.handoff import HandoffCoordinator, HandoffStore
from bankcua.observability.logging import RunLogger
from bankcua.safety.policy import Policy, PolicyEngine
from bankcua.surface.web_playwright import WebSurface
from bankcua.schema import (Checkpoint, InputParameter, OutputField,
                            ValueType)

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")


class ScriptedProvider:
    """Returns a fixed list of DiscoveryActions in order (stands in for the LLM)."""
    name = "scripted"

    def __init__(self, actions):
        self.actions = actions
        self.i = 0

    def decide(self, ctx):
        a = self.actions[min(self.i, len(self.actions) - 1)]
        self.i += 1
        return a


def _lookup_task(mock_app, max_seconds=300.0):
    return DiscoveryTask(
        capability_id="corebank.member_savings_lookup", name="lookup",
        description="d", goal="look up {member_id}", app_id="corebank",
        base_url=mock_app, entry_path="/login", vendor_product="Corebank",
        version="7.2.1", max_steps=14, max_seconds=max_seconds,
        inputs=[InputParameter(name="username", sensitive=True),
                InputParameter(name="password", sensitive=True),
                InputParameter(name="member_id")],
        outputs=[OutputField(name="member_name", type=ValueType.STRING),
                 OutputField(name="savings_balance", type=ValueType.MONEY)],
        success=Checkpoint(kind="text_present", value="Savings",
                           frame_path=["balancepane"]),
        param_values={"username": "operator", "password": "password123",
                      "member_id": "12345"})


_HAPPY = [
    DiscoveryAction(action="fill", ref=0, value="{username}", intent="user"),
    DiscoveryAction(action="fill", ref=1, value="{password}", intent="pass"),
    DiscoveryAction(action="click", ref=2, intent="sign on"),
    DiscoveryAction(action="fill", ref=0, value="{member_id}", intent="member id"),
    DiscoveryAction(action="click", ref=1, intent="search"),
    DiscoveryAction(action="extract", ref=2, output_name="member_name", intent="name"),
    DiscoveryAction(action="extract", ref=4, output_name="savings_balance", intent="bal"),
    DiscoveryAction(action="finish", success=True, intent="done"),
]


def _run(mock_app, tmp_path, task, actions):
    logger = RunLogger(str(tmp_path / "disc"), "discovery",
                       {"username", "password"}, ["operator", "password123"])
    pe = PolicyEngine(Policy.from_yaml(os.path.join(ROOT, "config/policy.yaml")))
    surf = WebSurface(task.base_url, headless=True)
    surf.start()
    coord = HandoffCoordinator(HandoffStore(str(tmp_path / "handoffs")), logger)
    try:
        return DiscoveryLoop(surf, ScriptedProvider(actions), pe, logger, coord).run(task)
    finally:
        surf.stop()


def test_discovery_happy_path_and_compile(mock_app, tmp_path):
    task = _lookup_task(mock_app)
    res = _run(mock_app, tmp_path, task, _HAPPY)
    assert res.status == "success"
    assert res.outputs["member_name"] == "Jane A. Doe"

    art = compile_artifact(task, res, evidence_dir=str(tmp_path / "art"),
                           recorded_by="scripted", discovery_run_id="t")
    # parameterisation: secrets -> secret_param, member_id -> param
    assert art.steps[0].value.kind == "secret_param"
    assert art.steps[3].value.kind == "param" and art.steps[3].value.param == "member_id"
    # extraction transforms + typed outputs
    ex = [s for s in art.steps if s.action.value == "extract"]
    assert any(s.extract.output == "savings_balance"
               and s.extract.transform == "money_to_cents" for s in ex)
    # known conditions attached from the vendor library; provenance set
    assert {c.code for c in art.known_conditions} >= {"MEMBER_NOT_FOUND", "SESSION_TIMEOUT"}
    assert art.provenance.recorded_by == "scripted"
    assert art.provenance.transcript_ref is not None


def test_discovery_escalate_action(mock_app, tmp_path):
    task = _lookup_task(mock_app)
    res = _run(mock_app, tmp_path, task,
               [DiscoveryAction(action="escalate", reason="need a human", intent="x")])
    assert res.status == "escalated"
    assert res.intervention_id is not None


def test_discovery_wall_clock_timeout(mock_app, tmp_path):
    task = _lookup_task(mock_app, max_seconds=0.0)   # deadline already passed
    res = _run(mock_app, tmp_path, task, _HAPPY)
    assert res.status == "timeout"


def test_discovery_stuck_no_progress(mock_app, tmp_path):
    task = _lookup_task(mock_app)
    # keep filling the same field on /login: URL never advances -> stuck
    stuck = [DiscoveryAction(action="fill", ref=0, value="x", intent="spin")] * 8
    res = _run(mock_app, tmp_path, task, stuck)
    assert res.status == "escalated"
    assert "progress" in res.reason.lower()


def test_discovery_finish_rejected_then_succeeds(mock_app, tmp_path):
    task = _lookup_task(mock_app)
    # finish immediately on /login (success checkpoint not met) -> rejected,
    # then run the real happy path
    actions = [DiscoveryAction(action="finish", success=True, intent="early"),
               *_HAPPY]
    res = _run(mock_app, tmp_path, task, actions)
    assert res.status == "success"


def test_discovery_policy_block_escalates(mock_app, tmp_path):
    task = _lookup_task(mock_app)
    # navigate to a denied route (policy blocks */logout) -> escalation
    res = _run(mock_app, tmp_path, task,
               [DiscoveryAction(action="navigate", url="/logout", intent="bad")])
    assert res.status == "escalated"


def test_discovery_unresolvable_target_escalates(mock_app, tmp_path):
    task = _lookup_task(mock_app)
    # the model references an element ref that does not exist -> escalate
    res = _run(mock_app, tmp_path, task,
               [DiscoveryAction(action="click", ref=999, intent="ghost")])
    assert res.status == "escalated"
    assert "unresolvable" in res.reason.lower()
