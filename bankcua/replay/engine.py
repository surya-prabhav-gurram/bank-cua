"""
Deterministic replay engine -- the production execution path.

Given an artifact + input params, re-run the recorded flow with NO LLM in the
decision loop. Determinism comes from:
  * Stable targeting: each element's ordered locator candidates are tried in
    order (surface._resolve); the flow survives one strategy going stale.
  * Explicit waits, not sleeps: each step waits for a declared condition.
  * Per-step checkpoints: every step asserts it actually reached the expected
    state before moving on -- no "assume the click worked."

Error handling is the core of this file. After each step we run the artifact's
KnownCondition detectors and act on the FIRST match:
  * business_outcome -> stop; return {status: business_outcome, code} (success
    of the *call*, just not the happy path).
  * recoverable      -> run the declared recovery (dismiss/reload/retry) up to
    its attempt budget, then re-evaluate and continue.
  * hard_failure     -> stop; return a structured {status: failure} with the
    step, what was expected, and what was observed.
Unrecognised trouble (a checkpoint that fails with no matching condition) is a
hard failure too, with rich evidence captured for debugging.

Irreversible steps flagged requires_confirmation are gated by policy: unattended
they raise an intervention (human-in-the-loop) rather than proceeding.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from ..safety.policy import Decision, PolicyEngine, PolicyViolation
from ..schema import (
    ActionType,
    CapabilityArtifact,
    Checkpoint,
    ConditionClass,
    KnownCondition,
    Step,
)
from ..surface.base import Surface
from .errors import apply_transform
from .result import (
    AssistEvent,
    BusinessOutcome,
    DriftSignal,
    FailureDetail,
    RecoveryEvent,
    ReplayResult,
    ReplayStatus,
)


class ReplayEngine:
    def __init__(self, surface: Surface, policy: PolicyEngine, logger,
                 coordinator=None, assist_provider=None, max_assists: int = 1,
                 escalate_unrecoverable: bool = False):
        self.surface = surface
        self.policy = policy
        self.logger = logger
        self.coordinator = coordinator
        # optional bounded assisted-recovery (task 15): a provider + a hard cap.
        self.assist_provider = assist_provider
        self.max_assists = max_assists
        # brief 3.6: a replay that hits a condition it can't recover from can
        # route a human intervention (needs a coordinator) instead of just
        # failing. Off by default so unattended runs fail fast.
        self.escalate_unrecoverable = escalate_unrecoverable
        self._drifts: list[DriftSignal] = []
        self._assists: list[AssistEvent] = []
        self._assist_used = 0
        self._params: dict = {}

    def run(self, art: CapabilityArtifact, params: dict[str, Any]) -> ReplayResult:
        t0 = time.time()
        art.validate_inputs(params)
        outputs: dict[str, Any] = {}
        recoveries: list[RecoveryEvent] = []
        self._params = params
        self._drifts, self._assists, self._assist_used = [], [], 0
        result = ReplayResult(status=ReplayStatus.FAILURE,
                              capability_id=art.id, version=art.version)

        self.logger.event("replay_started", capability=art.id, version=art.version,
                          num_steps=len(art.steps),
                          params={k: v for k, v in params.items()
                                  if k not in art.secret_params()})

        # Navigate to the tenant/entry binding first (this is part of target,
        # not a recorded step, so the same artifact re-points per tenant).
        entry = art.target.entry_path or "/"
        try:
            self.policy.check_url(art.target.base_url + entry)
        except PolicyViolation as pv:
            return ReplayResult(
                status=ReplayStatus.FAILURE, capability_id=art.id, version=art.version,
                duration_s=round(time.time() - t0, 2),
                failure=FailureDetail(code="POLICY_VIOLATION", expected="allowed entry",
                                      observed=str(pv)))
        self.surface.navigate(entry)
        self.logger.event("replay_navigated_entry", entry=entry)

        for step in art.steps:
            outcome = self._run_step(art, step, params, outputs, recoveries)
            if outcome is not None:            # terminal (business/failure/escalated)
                outcome = self._maybe_escalate(art, outcome)
                outcome.capability_id = art.id
                outcome.version = art.version
                outcome.outputs = outputs
                outcome.recoveries = recoveries
                outcome.drifts = self._drifts
                outcome.assists = self._assists
                outcome.steps_executed = step.index + 1
                outcome.duration_s = round(time.time() - t0, 2)
                self.logger.event("replay_finished", status=outcome.status.value,
                                  outputs={k: v for k, v in outputs.items()})
                return outcome

        # all steps done -> assert top-level success
        if self.surface.check(self._render_checkpoint(art.success, params)):
            result = ReplayResult(status=ReplayStatus.SUCCESS, capability_id=art.id,
                                  version=art.version, outputs=outputs,
                                  recoveries=recoveries, drifts=self._drifts,
                                  assists=self._assists, steps_executed=len(art.steps),
                                  duration_s=round(time.time() - t0, 2))
        else:
            self.logger.capture(self.surface, "final_checkpoint_failed", dom=True)
            result = ReplayResult(
                status=ReplayStatus.FAILURE, capability_id=art.id, version=art.version,
                outputs=outputs, recoveries=recoveries, drifts=self._drifts,
                assists=self._assists, steps_executed=len(art.steps),
                duration_s=round(time.time() - t0, 2),
                failure=FailureDetail(code="SUCCESS_CHECKPOINT_FAILED",
                                      expected=f"{art.success.kind}:{art.success.value}",
                                      observed=self.surface.current_url()))
            result = self._maybe_escalate(art, result)
        self.logger.event("replay_finished", status=result.status.value,
                          outputs={k: v for k, v in outputs.items()})
        return result

    # config/policy failures that should NOT trigger a human intervention
    _NON_ESCALATABLE = {"POLICY_VIOLATION", "STEP_BLOCKED_BY_POLICY",
                        "CONFIRMATION_REQUIRED", "ESCALATION_UNRESOLVED"}

    def _maybe_escalate(self, art, res: ReplayResult) -> ReplayResult:
        """Brief 3.6: on an UNRECOVERABLE runtime failure, route a human
        intervention (with full context, on the same live session) rather than
        just failing -- if a coordinator is present and this is enabled. The
        operator may salvage the run; otherwise the result becomes ESCALATED.
        No-op (returns the failure unchanged) for unattended runs."""
        if res.status != ReplayStatus.FAILURE or res.failure is None:
            return res
        if not (self.coordinator and self.escalate_unrecoverable):
            return res
        if res.failure.code in self._NON_ESCALATABLE:
            return res
        from ..escalation.handoff import InterventionKind, InterventionRequest
        caps = self.logger.capture(self.surface, "unrecoverable", dom=True)
        req = InterventionRequest(
            id=f"replay-{art.id}-unrecoverable-step{res.failure.step_index}",
            kind=InterventionKind.REPLAY_UNRECOVERABLE,
            reason=f"unrecoverable: {res.failure.code} at step "
                   f"{res.failure.step_index}",
            capability_id=art.id, current_step_index=res.failure.step_index,
            state_url=self.surface.current_url(),
            screenshot_path=caps.get("screenshot"), dom_path=caps.get("dom"),
            cdp_endpoint=getattr(self.surface, "cdp_endpoint", None))
        self.coordinator.raise_intervention(req)
        resolved = self.coordinator.wait_for_resolution(req.id)
        res.intervention_id = req.id
        if resolved.status.value == "resolved" and resolved.resume \
                and self.surface.check(self._render_checkpoint(art.success, self._params)):
            self.logger.event("unrecoverable_salvaged", id=req.id)
            return ReplayResult(status=ReplayStatus.SUCCESS, capability_id=art.id,
                                version=art.version, outputs=res.outputs,
                                intervention_id=req.id)
        res.status = ReplayStatus.ESCALATED
        return res

    # ------------------------------------------------------------------
    def _run_step(self, art, step: Step, params, outputs,
                  recoveries) -> Optional[ReplayResult]:
        # ---- safety pre-flight ------------------------------------------
        target_url = None
        if step.action == ActionType.NAVIGATE and step.url_template:
            target_url = self._render(step.url_template, params)
            target_url = target_url if target_url.startswith("http") \
                else art.target.base_url + target_url
        try:
            decision = self.policy.evaluate_step(step, target_url)
        except PolicyViolation as pv:
            return self._fail(step, "POLICY_VIOLATION", str(pv), "")

        skip_execution = False
        if decision.decision == Decision.BLOCK:
            return self._fail(step, "STEP_BLOCKED_BY_POLICY", decision.reason,
                              "run not approved for irreversible action")
        if decision.decision == Decision.NEEDS_CONFIRMATION:
            esc = self._confirm_or_escalate(art, step)
            if esc is not None:
                return esc            # aborted / timed out
            # resolved: operator may have performed the step manually
            skip_execution = self._operator_did_it(art, step)

        # ---- execute ----------------------------------------------------
        if not skip_execution:
            res = self._execute(art, step, params, outputs)
            self.logger.event("step_executed", index=step.index,
                              action=step.action.value, intent=step.intent,
                              ok=res.ok, message=res.message,
                              candidate_index=res.candidate_index)
            self._record_drift(step, res)
            if not res.ok and step.action != ActionType.EXTRACT:
                # execution failed -> maybe a known condition explains it
                cond_outcome = self._handle_conditions(art, step, recoveries)
                if cond_outcome is not None:
                    return cond_outcome
                # bounded, policy-checked single-step assisted recovery (opt-in)
                if not self._try_assist(art, step, params, outputs):
                    self.logger.capture(self.surface, f"step{step.index:02d}_failed",
                                        dom=True)
                    return self._fail(step, "ACTION_FAILED", "action to succeed",
                                      res.message)
                # assist succeeded -> fall through to checkpoint verification

        # ---- honour the step's declared wait before asserting ------------
        self._apply_wait(step)

        # ---- condition detection (business / recoverable / hard) --------
        cond_outcome = self._handle_conditions(art, step, recoveries)
        if cond_outcome is not None:
            return cond_outcome

        # ---- checkpoint verification ------------------------------------
        if step.checkpoint is not None:
            cp = self._render_checkpoint(step.checkpoint, params)
            if not self.surface.check(cp):
                # re-run detectors: a condition may have appeared
                cond_outcome = self._handle_conditions(art, step, recoveries)
                if cond_outcome is not None:
                    return cond_outcome
                caps = self.logger.capture(self.surface,
                                           f"step{step.index:02d}_checkpoint_failed",
                                           dom=True)
                return self._fail(
                    step, "CHECKPOINT_FAILED",
                    f"{cp.kind}:{cp.value}",
                    self.surface.current_url(), caps)
        return None   # continue to next step

    def _apply_wait(self, step: Step) -> None:
        """Honour a step's declared WaitSpec. 'load' is a no-op (navigate already
        waited); explicit selector/timeout/network_idle waits are applied so the
        wait strategy is real data, not decorative."""
        w = step.wait
        if not w:
            return
        if w.strategy == "selector" and w.target and hasattr(self.surface, "wait_for_selector"):
            self.surface.wait_for_selector(w.target, w.timeout_ms)
        elif w.strategy in ("timeout", "network_idle") and hasattr(self.surface, "wait_ms"):
            self.surface.wait_ms(min(w.timeout_ms, 500) if w.strategy == "network_idle"
                                 else w.timeout_ms)

    # ------------------------------------------------------------------
    def _execute(self, art, step: Step, params, outputs):
        s = self.surface
        if step.action == ActionType.NAVIGATE:
            url = self._render(step.url_template or "", params)
            return s.navigate(url)
        if step.action == ActionType.CLICK:
            return s.click(step.target)
        if step.action == ActionType.FILL:
            return s.fill(step.target, step.value.resolve(params, outputs))
        if step.action == ActionType.SELECT:
            return s.select_option(step.target, step.value.resolve(params, outputs),
                                   by=step.select_by)
        if step.action == ActionType.PRESS:
            return s.press(step.key or "Enter")
        if step.action == ActionType.WAIT_FOR:
            from ..surface.base import ActResult
            return ActResult(ok=True, message="wait")
        if step.action == ActionType.EXTRACT and step.extract:
            res = s.read(step.extract.locator, step.extract.attribute)
            if res.ok:
                outputs[step.extract.output] = apply_transform(
                    res.value, step.extract.transform)
            return res
        from ..surface.base import ActResult
        return ActResult(ok=True, message="noop")

    # ------------------------------------------------------------------
    def _handle_conditions(self, art, step, recoveries) -> Optional[ReplayResult]:
        """Detect known conditions and act on the first match."""
        for cond in art.known_conditions:
            if not self.surface.detect(cond.detector):
                continue
            self.logger.event("condition_detected", step=step.index,
                              code=cond.code, klass=cond.klass.value)
            if cond.klass == ConditionClass.BUSINESS_OUTCOME:
                self.logger.capture(self.surface, f"business_{cond.code}")
                return ReplayResult(
                    status=ReplayStatus.BUSINESS_OUTCOME, capability_id=art.id,
                    version=art.version,
                    business_outcome=BusinessOutcome(code=cond.code,
                                                     message=cond.message,
                                                     step_index=step.index))
            if cond.klass == ConditionClass.HARD_FAILURE:
                caps = self.logger.capture(self.surface, f"hard_{cond.code}", dom=True)
                return self._fail(step, cond.code, "flow to proceed", cond.message, caps)
            if cond.klass == ConditionClass.RECOVERABLE:
                ok = self._recover(cond, step, recoveries)
                if not ok:
                    caps = self.logger.capture(self.surface,
                                               f"recovery_failed_{cond.code}", dom=True)
                    return self._fail(step, f"RECOVERY_FAILED_{cond.code}",
                                      "recovery to succeed", cond.message, caps)
                # recovered -> re-scan in case dismissing revealed another condition
                return self._handle_conditions(art, step, recoveries)
        return None

    def _recover(self, cond: KnownCondition, step, recoveries) -> bool:
        rec = cond.recovery
        if rec is None:
            return False
        attempts = 0
        for attempts in range(1, rec.max_attempts + 1):
            self.logger.event("recovery_attempt", code=cond.code, kind=rec.kind,
                              attempt=attempts)
            if rec.kind == "click" and rec.target is not None:
                self.surface.click(rec.target)
            elif rec.kind == "reload":
                self.surface.navigate(self.surface.current_url())
            # else 'wait_retry': no action, just the backoff below
            if hasattr(self.surface, "wait_ms"):
                self.surface.wait_ms(rec.backoff_ms)
            # success = the condition signature is gone
            if not self.surface.detect(cond.detector):
                recoveries.append(RecoveryEvent(step_index=step.index,
                                  condition_code=cond.code, action=rec.kind,
                                  attempts=attempts, succeeded=True))
                return True
        recoveries.append(RecoveryEvent(step_index=step.index,
                          condition_code=cond.code, action=rec.kind,
                          attempts=attempts, succeeded=False))
        return False

    # ------------------------------------------------------------------
    def _confirm_or_escalate(self, art, step) -> Optional[ReplayResult]:
        """Gate an irreversible step behind a human. Returns a terminal result
        if the intervention was aborted/timed out, else None (proceed)."""
        if not self.coordinator:
            return self._fail(step, "CONFIRMATION_REQUIRED",
                              "human confirmation for irreversible step",
                              "no operator available (unattended, no coordinator)")
        from ..escalation.handoff import InterventionKind, InterventionRequest
        caps = self.logger.capture(self.surface, f"confirm_step{step.index:02d}", dom=True)
        req = InterventionRequest(
            id=f"replay-{art.id}-step{step.index}",
            kind=InterventionKind.RISKY_CONFIRMATION,
            reason=f"irreversible step '{step.intent}' needs human confirmation",
            capability_id=art.id, current_step_index=step.index,
            state_url=self.surface.current_url(),
            screenshot_path=caps.get("screenshot"), dom_path=caps.get("dom"),
            cdp_endpoint=getattr(self.surface, "cdp_endpoint", None))
        self.coordinator.raise_intervention(req)
        resolved = self.coordinator.wait_for_resolution(req.id)
        if resolved.status.value != "resolved" or not resolved.resume:
            return ReplayResult(status=ReplayStatus.ESCALATED, capability_id=art.id,
                                version=art.version, intervention_id=req.id,
                                failure=FailureDetail(
                                    code="ESCALATION_UNRESOLVED", step_index=step.index,
                                    expected="operator to approve/perform",
                                    observed=resolved.status.value))
        self.logger.event("confirmation_granted", id=req.id,
                          note=resolved.resolution_note)
        return None

    def _operator_did_it(self, art, step) -> bool:
        """After a resolved confirmation, did the operator perform the step
        manually (so we should skip execution)?"""
        if not self.coordinator:
            return False
        try:
            req = self.coordinator.store.read(f"replay-{art.id}-step{step.index}")
            return "manual" in (req.resolution_note or "").lower() or bool(req.human_actions)
        except Exception:
            return False

    # ------------------------------------------------------------------
    def _record_drift(self, step: Step, res) -> None:
        """Note when a non-primary locator candidate had to be used."""
        if step.target is None or res is None:
            return
        ci = getattr(res, "candidate_index", None)
        if ci is not None and ci > 0:
            kind = ""
            try:
                kind = step.target.candidates[ci].kind.value
            except Exception:
                pass
            self._drifts.append(DriftSignal(step_index=step.index,
                                            description=step.target.description,
                                            candidate_index=ci, kind=kind))
            self.logger.event("locator_drift", step=step.index, candidate_index=ci,
                              kind=kind, description=step.target.description)

    def _try_assist(self, art, step, params, outputs) -> bool:
        """Bounded, policy-checked single-step LLM recovery. Never open-ended:
        at most `max_assists` per run, one action, only for the failing step."""
        if not self.assist_provider or self._assist_used >= self.max_assists:
            return False
        from ..agent.providers import DecisionContext
        from ..agent.loop import (infer_risk, resolve_placeholders,
                                  _ACTION_TO_TYPE)
        from ..schema import ActionType as AT
        self._assist_used += 1
        obs = self.surface.observe()
        shot = None
        try:
            shot = self.logger.capture(self.surface,
                                       f"assist_step{step.index:02d}").get("screenshot")
        except Exception:
            pass
        ctx = DecisionContext(
            goal=(f"A recorded replay step failed. Its intent was: "
                  f"'{step.intent}'. Choose ONE action "
                  f"(click/fill/select/press/navigate) that accomplishes exactly "
                  f"this step on the CURRENT screen. Do not go beyond this step."),
            inputs_hint="\n".join(f"- {p.name}" for p in art.inputs),
            outputs_hint="", observation_text=obs.render_for_model(),
            history="", screenshot_path=shot, step_index=step.index)
        try:
            action = self.assist_provider.decide(ctx)
        except Exception as ex:
            self.logger.event("assist_error", step=step.index, error=str(ex))
            return False
        if action.action not in ("click", "fill", "select", "press", "navigate"):
            self.logger.event("assist_rejected", step=step.index,
                              action=action.action, reason="non-actionable")
            return False
        elem = (next((e for e in obs.elements if e.ref == action.ref), None)
                if action.ref is not None else None)
        loc = self.surface.locator_for_element(elem) if elem else None
        risk = infer_risk(elem, action.action)
        url = (action.url if action.action == "navigate"
               else self.surface.current_url())
        full = url if url.startswith("http") else art.target.base_url + (url or "")
        try:
            dec = self.policy.evaluate_discovery_action(
                _ACTION_TO_TYPE.get(action.action, AT.CLICK), full, risk)
        except PolicyViolation as pv:
            self.logger.event("assist_policy_block", step=step.index, reason=str(pv))
            return False
        if dec.decision != Decision.ALLOW:
            self.logger.event("assist_policy_block", step=step.index, reason=dec.reason)
            return False
        r = None
        if action.action == "navigate":
            r = self.surface.navigate(resolve_placeholders(action.url or "", params))
        elif action.action == "click" and loc:
            r = self.surface.click(loc)
        elif action.action == "fill" and loc:
            r = self.surface.fill(loc, resolve_placeholders(action.value or "", params))
        elif action.action == "select" and loc:
            r = self.surface.select_option(
                loc, resolve_placeholders(action.value or "", params),
                by=action.select_by)
        elif action.action == "press":
            r = self.surface.press(action.key or "Enter")
        ok = bool(r and r.ok)
        self._assists.append(AssistEvent(step_index=step.index, action=action.action,
                                         intent=action.intent, succeeded=ok))
        self.logger.event("assist_applied", step=step.index, action=action.action, ok=ok)
        return ok

    @staticmethod
    def _render(template: str, params) -> str:
        out = template
        for k, v in params.items():
            out = out.replace("{" + k + "}", str(v))
        return out

    def _render_checkpoint(self, cp: Checkpoint, params) -> Checkpoint:
        return cp.model_copy(update={"value": self._render(cp.value, params)})

    def _fail(self, step, code, expected, observed, evidence=None) -> ReplayResult:
        self.logger.event("hard_failure", step=getattr(step, "index", None),
                          code=code, expected=expected, observed=observed)
        return ReplayResult(
            status=ReplayStatus.FAILURE, capability_id="", version="",
            failure=FailureDetail(code=code, step_index=getattr(step, "index", None),
                                  expected=expected, observed=observed,
                                  evidence=evidence or {}))
