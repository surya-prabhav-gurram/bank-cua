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

Declared outputs are guaranteed: a run cannot report SUCCESS while an output
the artifact promised the caller is missing (OUTPUT_EXTRACTION_FAILED).

Irreversible steps flagged requires_confirmation are gated by policy: unattended
they raise an intervention (human-in-the-loop) rather than proceeding.
"""
from __future__ import annotations

import time
from typing import Any, ClassVar, Optional

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
    Refusal,
    ReplayResult,
    ReplayStatus,
)


class ReplayEngine:
    def __init__(self, surface: Surface, policy: PolicyEngine, logger,
                 coordinator=None, assist_provider=None, max_assists: int = 1,
                 escalate_unrecoverable: bool = False,
                 initiator: str = "", approver: str = "", ledger=None):
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
        # Dual control: who asked for this run, and who (if anyone) counter-signed.
        # They must be different people -- a run may not approve itself.
        self.initiator = initiator
        self.approver = approver
        # Memory for velocity limits. Without it a per-invocation ceiling is
        # blind to ten near-limit runs in a row.
        self.ledger = ledger
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

        # ---- value-level (semantic) policy --------------------------------
        # Runs before the browser is even pointed anywhere: the cheapest place to
        # refuse a $1M transfer is before it has been typed into anything.
        try:
            vdec = self.policy.evaluate_inputs(params, ledger=self.ledger)
        except PolicyViolation as pv:
            self.logger.event("value_policy_block", reason=str(pv))
            return self._as_refusal(ReplayResult(
                status=ReplayStatus.FAILURE, capability_id=art.id,
                version=art.version, duration_s=round(time.time() - t0, 2),
                failure=FailureDetail(code="VALUE_LIMIT_EXCEEDED",
                                      expected="inputs within policy limits",
                                      observed=str(pv))))
        if vdec.decision == Decision.NEEDS_CONFIRMATION:
            dual = self._satisfy_dual_control(art, vdec)
            if dual is not None:
                dual = self._as_refusal(dual)
                dual.duration_s = round(time.time() - t0, 2)
                self.logger.event("replay_finished", status=dual.status.value)
                return dual

        # Navigate to the tenant/entry binding first (this is part of target,
        # not a recorded step, so the same artifact re-points per tenant).
        entry = art.target.entry_path or "/"
        try:
            self.policy.check_url(art.target.base_url + entry)
        except PolicyViolation as pv:
            return self._as_refusal(ReplayResult(
                status=ReplayStatus.FAILURE, capability_id=art.id, version=art.version,
                duration_s=round(time.time() - t0, 2),
                failure=FailureDetail(code="POLICY_VIOLATION", expected="allowed entry",
                                      observed=str(pv))))
        self.surface.navigate(entry)
        self.logger.event("replay_navigated_entry", entry=entry)

        for step in art.steps:
            outcome = self._run_step(art, step, params, outputs, recoveries)
            if outcome is not None:            # terminal (business/failure/escalated)
                outcome = self._as_refusal(self._maybe_escalate(art, outcome))
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

        # all steps done -> every DECLARED output must actually be populated.
        # The artifact is a CONTRACT: returning success while an output the
        # caller was promised is missing would be a silent breach -- worse than
        # an error, because nothing downstream knows to check. Note the split of
        # responsibility: a failed extract is deliberately NOT fatal at the step
        # (so the condition detectors still get their chance to explain *why*
        # the value was absent -- e.g. PERMISSION_DENIED), but the contract is
        # enforced here, once, at the end of the run.
        missing = [o.name for o in art.outputs if o.name not in outputs]
        if missing:
            # blame the step that was supposed to produce the first missing
            # output, so the failure points somewhere debuggable rather than
            # "somewhere in the run"
            blame = next((st.index for st in art.steps
                          if st.extract and st.extract.output == missing[0]),
                         len(art.steps) - 1)
            caps = self.logger.capture(self.surface, "outputs_missing", dom=True)
            result = ReplayResult(
                status=ReplayStatus.FAILURE, capability_id=art.id,
                version=art.version, outputs=outputs, recoveries=recoveries,
                drifts=self._drifts, assists=self._assists,
                steps_executed=len(art.steps),
                duration_s=round(time.time() - t0, 2),
                failure=FailureDetail(
                    code="OUTPUT_EXTRACTION_FAILED", step_index=blame,
                    expected=f"declared outputs {missing} to be populated",
                    observed=f"populated: {sorted(outputs)}",
                    evidence=caps))
            result = self._as_refusal(self._maybe_escalate(art, result))
            self.logger.event("replay_finished", status=result.status.value,
                              outputs={k: v for k, v in outputs.items()})
            return result

        # all steps done -> assert top-level success
        if self.surface.check(self._render_checkpoint(art.success, params)):
            self._record_value_movement(art, params)
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
            result = self._as_refusal(self._maybe_escalate(art, result))
        self.logger.event("replay_finished", status=result.status.value,
                          outputs={k: v for k, v in outputs.items()})
        return result

    # config/policy failures that should NOT trigger a human intervention
    #: A guardrail declined. Nothing broke: the request was refused, and the
    #: caller's move is to change the REQUEST -- a smaller amount, a second
    #: approver, an approved capability -- not to investigate the system.
    _REFUSAL_CODES: ClassVar[set[str]] = {
        "POLICY_VIOLATION", "STEP_BLOCKED_BY_POLICY", "CONFIRMATION_REQUIRED",
        "VALUE_LIMIT_EXCEEDED", "DUAL_CONTROL_REQUIRED",
    }
    #: Refusals plus the already-escalated case. None of these should wake a
    #: human at 3am: a person cannot fix a policy file from a browser.
    _NON_ESCALATABLE: ClassVar[set[str]] = _REFUSAL_CODES | {"ESCALATION_UNRESOLVED"}

    def _record_value_movement(self, art, params) -> None:
        """Book governed amounts to the ledger -- only on success.

        A refused, escalated or failed run moved no money, so charging it against
        the velocity budget would starve the budget with runs that never happened.
        """
        if self.ledger is None:
            return
        from ..safety.ledger import LedgerEntry
        import time as _t
        for name in getattr(self.policy.policy, "value_rules", {}):
            if name not in params:
                continue
            amount = self.policy._as_number(params[name])
            if amount is None or amount != amount:
                continue
            self.ledger.record(LedgerEntry(
                ts=_t.time(), capability_id=art.id, param=name, value=amount,
                initiator=self.initiator, approver=self.approver))
            self.logger.event("value_recorded", param=name, value=amount,
                              capability=art.id)

    def _as_refusal(self, res: ReplayResult) -> ReplayResult:
        """Retype a policy decision from FAILURE to REFUSED.

        Applied at the boundary rather than at each guardrail so there is exactly
        one place that decides what counts as a refusal -- the same set that
        decides what must not page a human.
        """
        if res.status != ReplayStatus.FAILURE or res.failure is None:
            return res
        if res.failure.code not in self._REFUSAL_CODES:
            return res
        res.refusal = Refusal(code=res.failure.code,
                              requirement=res.failure.expected,
                              reason=res.failure.observed,
                              step_index=res.failure.step_index,
                              evidence=res.failure.evidence)
        res.failure = None
        res.status = ReplayStatus.REFUSED
        self.logger.event("replay_refused", code=res.refusal.code,
                          requirement=res.refusal.requirement)
        return res

    def _satisfy_dual_control(self, art, vdec) -> Optional[ReplayResult]:
        """Resolve a dual-control requirement. Returns None to proceed, or a
        terminal result.

        Three ways this ends, in order of preference:
          * a named second approver was supplied and is a different person ->
            proceed, recorded;
          * an operator is reachable -> raise an intervention carrying the exact
            parameters that tripped the threshold, and let a human counter-sign;
          * nobody is home -> refuse. Unattended is precisely when a second pair
            of eyes cannot be assumed, so this fails closed.
        """
        if self.policy.approver_is_independent(self.approver, self.initiator):
            self.logger.event("dual_control_satisfied", approver=self.approver,
                              initiator=self.initiator, params=list(vdec.params),
                              reason=vdec.reason)
            return None
        if self.approver and not self.policy.approver_is_independent(
                self.approver, self.initiator):
            self.logger.event("dual_control_rejected", reason="approver is the initiator")
        if not self.coordinator:
            self.logger.event("dual_control_unmet", reason=vdec.reason)
            return ReplayResult(
                status=ReplayStatus.FAILURE, capability_id=art.id,
                version=art.version,
                failure=FailureDetail(
                    code="DUAL_CONTROL_REQUIRED",
                    expected="an independent second approver",
                    observed=vdec.reason))
        from ..escalation.handoff import InterventionKind, InterventionRequest
        req = InterventionRequest(
            id=f"replay-{art.id}-dualcontrol",
            kind=InterventionKind.DUAL_CONTROL,
            reason=vdec.reason, capability_id=art.id,
            state_url=self.surface.current_url(),
            cdp_endpoint=getattr(self.surface, "cdp_endpoint", None))
        self.coordinator.raise_intervention(req)
        resolved = self.coordinator.wait_for_resolution(req.id)
        if resolved.status.value == "resolved" and resolved.resume:
            self.logger.event("dual_control_granted", id=req.id,
                              note=resolved.resolution_note)
            return None
        return ReplayResult(
            status=ReplayStatus.ESCALATED, capability_id=art.id,
            version=art.version, intervention_id=req.id,
            failure=FailureDetail(code="DUAL_CONTROL_REQUIRED",
                                  expected="a second reviewer to counter-sign",
                                  observed=resolved.status.value))

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
            res = self._execute(step, params, outputs)
            self.logger.event("step_executed", index=step.index,
                              action=step.action.value, intent=step.intent,
                              ok=res.ok, message=res.message,
                              candidate_index=res.candidate_index)
            self._record_drift(step, res)
            # A fill's effect is on the CONTROL, not the page, so a page-state
            # checkpoint cannot see it. Read the value back instead: this is what
            # catches a readonly/disabled/JS-managed input that silently swallows
            # the write and leaves the flow running on empty data.
            if res.ok and step.action == ActionType.FILL and step.verify_value:
                bad = self._verify_fill(art, step, params, outputs, recoveries)
                if bad is not None:
                    return bad
            if not res.ok and step.action != ActionType.EXTRACT:
                # execution failed -> maybe a known condition explains it
                cond_outcome = self._handle_conditions(art, step, recoveries, outputs)
                if cond_outcome is not None:
                    return cond_outcome
                # bounded, policy-checked single-step assisted recovery (opt-in)
                if not self._try_assist(art, step, params):
                    caps = self.logger.capture(
                        self.surface, f"step{step.index:02d}_failed", dom=True)
                    return self._fail(step, "ACTION_FAILED", "action to succeed",
                                      res.message, caps)
                # assist succeeded -> fall through to checkpoint verification

        # ---- honour the step's declared wait before asserting ------------
        self._apply_wait(step)

        # ---- condition detection (business / recoverable / hard) --------
        cond_outcome = self._handle_conditions(art, step, recoveries, outputs)
        if cond_outcome is not None:
            return cond_outcome

        # ---- checkpoint verification ------------------------------------
        if step.checkpoint is not None:
            cp = self._render_checkpoint(step.checkpoint, params)
            if not self.surface.check(cp):
                # re-run detectors: a condition may have appeared
                cond_outcome = self._handle_conditions(art, step, recoveries, outputs)
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

    def _verify_fill(self, art, step: Step, params, outputs,
                     recoveries) -> Optional[ReplayResult]:
        """Read a filled control back and assert the write landed.

        A secret is asserted NON-EMPTY only -- never compared, never logged. We
        already refuse to let a credential reach an observation or a log; reading
        one back to diff it would reintroduce exactly that leak for the sake of a
        stricter assertion nobody needs."""
        intended = step.value.resolve(params, outputs) if step.value else ""
        secret = bool(step.value and step.value.kind == "secret_param")
        back = self.surface.read(step.target, "value")
        if back.ok:
            got = (back.value or "").strip()
            if (got != "") if secret else (got == str(intended).strip()):
                return None
        # a known condition (validation error, session gone) may explain it
        cond = self._handle_conditions(art, step, recoveries, outputs)
        if cond is not None:
            return cond
        caps = self.logger.capture(self.surface,
                                   f"step{step.index:02d}_fill_not_applied", dom=True)
        self.logger.event("fill_not_applied", step=step.index, secret=secret)
        return self._fail(
            step, "FILL_NOT_APPLIED",
            "the control to hold a non-empty value" if secret
            else f"the control to hold {intended!r}",
            "read-back was empty or different (control may be readonly, disabled, "
            "or JS-managed)", caps)

    def _surface_outcome_outputs(self, art, outputs) -> list[str]:
        """Best-effort read of declared outputs still visible on a business-outcome
        screen (KnownCondition.surfaces_outputs).

        "Permission denied" can legitimately still tell you *whose* account it
        was; "not found" tells you nothing. Which of the two a condition is, is a
        property of the vendor's UI, so it is declared per condition in the
        knowledge library rather than guessed at runtime. Failures here are silent
        by construction -- this is extra data on a non-success, never a reason to
        turn one into an error."""
        gained: list[str] = []
        for st in art.steps:
            if not st.extract or st.extract.output in outputs:
                continue
            try:
                res = self.surface.read(st.extract.locator, st.extract.attribute)
                if res.ok and (res.value or "").strip():
                    outputs[st.extract.output] = apply_transform(
                        res.value, st.extract.transform)
                    gained.append(st.extract.output)
            except Exception:
                continue
        if gained:
            self.logger.event("outcome_outputs_surfaced", outputs=gained)
        return gained

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
    def _execute(self, step: Step, params, outputs):
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
    def _handle_conditions(self, art, step, recoveries,
                           outputs=None) -> Optional[ReplayResult]:
        """Detect known conditions and act on the first match."""
        for cond in art.known_conditions:
            if not self.surface.detect(cond.detector):
                continue
            self.logger.event("condition_detected", step=step.index,
                              code=cond.code, klass=cond.klass.value)
            if cond.klass == ConditionClass.BUSINESS_OUTCOME:
                self.logger.capture(self.surface, f"business_{cond.code}")
                # A legitimate non-success can still carry data the caller needs.
                surfaced = (self._surface_outcome_outputs(art, outputs)
                            if cond.surfaces_outputs and outputs is not None else [])
                return ReplayResult(
                    status=ReplayStatus.BUSINESS_OUTCOME, capability_id=art.id,
                    version=art.version,
                    business_outcome=BusinessOutcome(code=cond.code,
                                                     message=cond.message,
                                                     step_index=step.index,
                                                     outputs_surfaced=surfaced))
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
                return self._handle_conditions(art, step, recoveries, outputs)
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
                              "no human reviewer available (unattended, no coordinator)")
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
                                    expected="a human reviewer to approve or perform the step",
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

    def _try_assist(self, art, step, params) -> bool:
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
