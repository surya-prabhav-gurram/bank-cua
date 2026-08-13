"""
Goal-driven discovery loop: observe -> decide -> act against a live surface.

Responsibilities:
  * Drive the surface using the provider's decisions until the goal's success
    checkpoint holds, or a stopping condition fires (max steps / timeout / dead-end / policy).
  * Enforce safety on every action BEFORE it happens (allowlist + risk).
  * Record a structured transcript (the compiler turns it into an artifact) and
    emit redacted evidence via the logger.
  * Escalate to a human when stuck or when the model proposes a risky action it
    isn't approved for.

The loop is deliberately model-agnostic (see providers.py) and surface-agnostic
(see surface/base.py).
"""
from __future__ import annotations

import re
import time
from typing import Optional

from pydantic import BaseModel, Field

from ..safety.policy import Decision, PolicyEngine, PolicyViolation
from ..schema import ActionType, Locator, RiskClass
from ..surface.base import ElementInfo, Surface
from .providers import DecisionContext, LLMProvider
from .task import DiscoveryTask

_RISKY_WORDS = re.compile(r"\b(confirm|create|submit|delete|remove|transfer|"
                          r"post|approve|authorize|pay|send)\b", re.I)


def infer_risk(elem: Optional[ElementInfo], action: str) -> RiskClass:
    if action in ("navigate", "extract", "press", "fill", "select"):
        return RiskClass.SAFE
    if action == "click" and elem is not None:
        blob = f"{elem.name} {elem.text} {elem.label}".strip()
        if _RISKY_WORDS.search(blob):
            return RiskClass.RISKY
    return RiskClass.SAFE


def resolve_placeholders(text: Optional[str], param_values: dict) -> Optional[str]:
    """Substitute {param} placeholders with real values (kept out of the model)."""
    if text is None:
        return None
    out = text
    for k, v in param_values.items():
        out = out.replace("{" + k + "}", str(v))
    return out


_ACTION_TO_TYPE = {
    "navigate": ActionType.NAVIGATE, "click": ActionType.CLICK,
    "fill": ActionType.FILL, "select": ActionType.SELECT,
    "press": ActionType.PRESS, "extract": ActionType.EXTRACT,
}


class TranscriptStep(BaseModel):
    index: int
    intent: str = ""
    action_kind: str
    url: str = ""
    url_template: Optional[str] = None
    element: Optional[ElementInfo] = None
    locator: Optional[Locator] = None
    value_raw: Optional[str] = None
    select_by: str = "label"
    key: Optional[str] = None
    output_name: Optional[str] = None
    attribute: str = "text"
    extracted_value: Optional[str] = None
    risk: RiskClass = RiskClass.SAFE
    ok: bool = True
    message: str = ""
    candidate_index: Optional[int] = None


class DiscoveryResult(BaseModel):
    status: str          # success | escalated | failed | max_steps | timeout
    reason: str = ""
    transcript: list[TranscriptStep] = Field(default_factory=list)
    outputs: dict[str, str] = Field(default_factory=dict)
    intervention_id: Optional[str] = None


class DiscoveryLoop:
    def __init__(self, surface: Surface, provider: LLMProvider,
                 policy: PolicyEngine, logger, coordinator=None):
        self.surface = surface
        self.provider = provider
        self.policy = policy
        self.logger = logger
        self.coordinator = coordinator

    def _inputs_hint(self, task: DiscoveryTask) -> str:
        lines = []
        for p in task.inputs:
            val = task.param_values.get(p.name)
            shown = "***(provided secret)***" if p.sensitive else repr(val)
            lines.append(f"- {p.name} ({p.type.value}): {shown}")
        return "\n".join(lines) or "(none)"

    def _outputs_hint(self, task: DiscoveryTask) -> str:
        return "\n".join(f"- {o.name} ({o.type.value}): {o.description}"
                         for o in task.outputs) or "(none)"

    def run(self, task: DiscoveryTask) -> DiscoveryResult:
        transcript: list[TranscriptStep] = []
        outputs: dict[str, str] = {}
        history: list[str] = []
        # static for the whole task -> build once, not per step
        inputs_hint = self._inputs_hint(task)
        outputs_hint = self._outputs_hint(task)
        goal = task.rendered_goal()

        self.surface.navigate(task.entry_path)
        self.logger.event("discovery_started", goal=goal,
                          provider=self.provider.name, entry=task.entry_path)

        stuck_urls: list[str] = []
        deadline = time.time() + task.max_seconds

        for i in range(task.max_steps):
            # wall-clock stopping condition (alongside max_steps and dead-end)
            if time.time() > deadline:
                self.logger.event("discovery_timeout", step=i,
                                  max_seconds=task.max_seconds)
                return DiscoveryResult(status="timeout",
                                       reason=f"wall-clock timeout after "
                                              f"{task.max_seconds}s",
                                       transcript=transcript, outputs=outputs)
            obs = self.surface.observe()
            shot = self.logger.capture(self.surface, f"step{i:02d}").get("screenshot")
            self.logger.event("observation", step=i, url=obs.url,
                              status=obs.http_status,
                              num_elements=len(obs.elements))

            ctx = DecisionContext(
                goal=goal, inputs_hint=inputs_hint, outputs_hint=outputs_hint,
                observation_text=obs.render_for_model(),
                history="\n".join(history[-6:]),
                screenshot_path=shot, step_index=i)
            action = self.provider.decide(ctx)
            self.logger.event("model_decided", step=i,
                              action=action.action, intent=action.intent,
                              ref=action.ref, value=action.value)

            # ---- terminal actions -------------------------------------
            if action.action == "finish":
                ok = self.surface.check(task.success)
                self.logger.event("finish_requested", success_checkpoint=ok,
                                  model_success=action.success)
                if ok:
                    return DiscoveryResult(status="success", reason=action.reason,
                                           transcript=transcript, outputs=outputs)
                history.append(f"[{i}] finish rejected: success checkpoint not met")
                continue
            if action.action == "escalate":
                iid = self._escalate(task, i, action.reason or "model escalated", obs)
                return DiscoveryResult(status="escalated", reason=action.reason,
                                       transcript=transcript, outputs=outputs,
                                       intervention_id=iid)

            # ---- resolve target (interactive element or read-only field) ----
            elem: Optional[ElementInfo] = None
            loc: Optional[Locator] = None
            if action.ref is not None:
                elem = next((e for e in obs.elements if e.ref == action.ref), None)
                if elem is not None:
                    loc = self.surface.locator_for_element(elem)
                else:
                    rf = next((r for r in obs.readouts if r.ref == action.ref), None)
                    if rf is not None:
                        loc = rf.locator
            risk = infer_risk(elem, action.action)

            # ---- unresolvable target -> escalate ----------------------
            if action.action in ("click", "fill", "select", "extract") and loc is None:
                iid = self._escalate(
                    task, i, f"unresolvable target (ref {action.ref})", obs)
                return DiscoveryResult(status="escalated",
                                       reason=f"unresolvable target (ref {action.ref})",
                                       transcript=transcript, outputs=outputs,
                                       intervention_id=iid)

            # ---- safety pre-flight ------------------------------------
            target_url = (action.url if action.action == "navigate"
                          else self.surface.current_url())
            try:
                atype = _ACTION_TO_TYPE.get(action.action, ActionType.CLICK)
                decision = self.policy.evaluate_discovery_action(
                    atype, target_url if target_url.startswith("http")
                    else self.surface.base_url + target_url, risk)
            except PolicyViolation as pv:
                self.logger.event("policy_block", step=i, reason=str(pv))
                iid = self._escalate(task, i, f"policy violation: {pv}", obs)
                return DiscoveryResult(status="escalated", reason=str(pv),
                                       transcript=transcript, outputs=outputs,
                                       intervention_id=iid)
            if decision.decision == Decision.NEEDS_CONFIRMATION:
                self.logger.event("risky_action_escalation", step=i,
                                  reason=decision.reason)
                iid = self._escalate(task, i, decision.reason, obs)
                return DiscoveryResult(status="escalated", reason=decision.reason,
                                       transcript=transcript, outputs=outputs,
                                       intervention_id=iid)

            # ---- execute ----------------------------------------------
            rec = TranscriptStep(index=len(transcript), intent=action.intent,
                                 action_kind=action.action, url=obs.url,
                                 element=elem, locator=loc, risk=risk,
                                 select_by=action.select_by, key=action.key,
                                 output_name=action.output_name,
                                 attribute=action.attribute)
            if action.action == "navigate":
                resolved_url = resolve_placeholders(action.url, task.param_values) or ""
                rec.url_template = resolved_url
                res = self.surface.navigate(resolved_url)
            elif action.action == "click":
                res = self.surface.click(loc) if loc else _noloc()
            elif action.action == "fill":
                rec.value_raw = resolve_placeholders(action.value, task.param_values) or ""
                res = self.surface.fill(loc, rec.value_raw) if loc else _noloc()
            elif action.action == "select":
                rec.value_raw = resolve_placeholders(action.value, task.param_values) or ""
                res = self.surface.select_option(loc, rec.value_raw,
                                                 by=action.select_by) if loc else _noloc()
            elif action.action == "press":
                res = self.surface.press(action.key or "Enter")
            elif action.action == "extract":
                res = self.surface.read(loc, action.attribute) if loc else _noloc()
                if res.ok and action.output_name:
                    outputs[action.output_name] = res.value or ""
                    rec.extracted_value = res.value
            else:
                res = _noloc()

            rec.ok, rec.message = res.ok, res.message
            rec.candidate_index = res.candidate_index
            transcript.append(rec)
            self.logger.event("action_executed", step=i, kind=action.action,
                              ok=res.ok, message=res.message,
                              candidate_index=res.candidate_index)
            history.append(f"[{i}] {action.action} ({action.intent}) -> "
                           f"{'ok' if res.ok else 'FAIL: ' + res.message}")

            # ---- stuck detection --------------------------------------
            stuck_urls.append(self.surface.current_url())
            if len(stuck_urls) >= 4 and len(set(stuck_urls[-4:])) == 1 \
                    and action.action != "extract":
                iid = self._escalate(task, i, "no navigation progress in 4 steps", obs)
                return DiscoveryResult(status="escalated",
                                       reason="stuck: no progress",
                                       transcript=transcript, outputs=outputs,
                                       intervention_id=iid)

        # exhausted steps
        if self.surface.check(task.success):
            return DiscoveryResult(status="success",
                                   reason="success checkpoint met at max steps",
                                   transcript=transcript, outputs=outputs)
        return DiscoveryResult(status="max_steps", reason="max steps exhausted",
                               transcript=transcript, outputs=outputs)

    def _escalate(self, task, step_index, reason, obs) -> Optional[str]:
        if not self.coordinator:
            self.logger.event("escalation_no_coordinator", reason=reason)
            return None
        from ..escalation.handoff import (InterventionKind, InterventionRequest)
        caps = self.logger.capture(self.surface, f"escalation_step{step_index:02d}",
                                   dom=True)
        req = InterventionRequest(
            id=f"disc-{task.capability_id}-{step_index}",
            kind=InterventionKind.DISCOVERY_STUCK, reason=reason,
            capability_id=task.capability_id, goal=task.rendered_goal(),
            current_step_index=step_index, state_url=obs.url,
            screenshot_path=caps.get("screenshot"), dom_path=caps.get("dom"),
            cdp_endpoint=getattr(self.surface, "cdp_endpoint", None))
        self.coordinator.raise_intervention(req)
        return req.id


def _noloc():
    from ..surface.base import ActResult
    return ActResult(ok=False, message="no element resolved for action")
