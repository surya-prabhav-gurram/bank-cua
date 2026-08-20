"""
Safety & policy guardrails.

Enforced in BOTH the discovery loop and deterministic replay, on every action,
before it happens. Three concerns:

  1. Allowlist. The agent may only touch URLs on an explicit allowlist and may
     only perform allowed action *types*. Anything else is blocked hard. The
     allowlist is layered: a global policy (config/policy.yaml) AND the
     artifact's own `target.allowed_url_patterns` -- a URL must satisfy both.

  2. Risk of irreversibility. Reversible actions (navigate/read/fill/select) are
     `safe`. Actions with side effects that cannot be undone (submit/create/
     confirm/delete) are `risky`. Risky actions are handled conservatively:
     blocked unless the run is explicitly approved to perform them
     (`allow_risky`) OR the step is flagged `requires_confirmation`, in which
     case they are gated behind a human confirmation (which, unattended, becomes
     an escalation -- see escalation/handoff.py).

  3. Value-level (semantic) policy. The allowlist is URL/action-shaped: it can
     tell "may I click here" but not "is this amount sane." So a third layer
     inspects the *inputs* a capability is invoked with, before the browser opens:
     per-parameter ceilings (a hard refusal) and dual-control thresholds (a second
     named approver, or an escalation). This is the layer that distinguishes
     transferring $1 from transferring $1M -- the limit the first version of this
     system explicitly did not have.

  4. It is advisory-free: a violation raises, it does not warn-and-continue.

Why this shape: in a bank, "accidentally created a sub-account" is far worse
than "refused to create one." Failing closed on the irreversible class is the
correct default; the write-up (section 6) covers the limits.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import yaml

from ..schema import ActionType, RiskClass, Step


class PolicyViolation(Exception):
    """Raised when an action is not permitted. Fails closed."""


class Decision(str, Enum):
    ALLOW = "allow"
    NEEDS_CONFIRMATION = "needs_confirmation"  # gate -> human/escalation
    BLOCK = "block"


@dataclass
class PolicyDecision:
    decision: Decision
    reason: str = ""
    # for value-level decisions: which input parameters drove it
    params: tuple[str, ...] = ()


@dataclass
class ValueRule:
    """Semantic limits on one input parameter's value.

    `max` is a hard ceiling -- exceeding it is refused outright, never escalated,
    because no operator standing at a browser should be able to wave through an
    amount the institution has already said is out of bounds.

    `dual_control_above` is the softer band: permitted, but not by one person
    acting alone. It resolves to a second named approver or, unattended, to an
    intervention."""
    max: Optional[float] = None
    dual_control_above: Optional[float] = None
    unit: str = ""
    # Aggregate band. A per-invocation ceiling is blind to velocity: ten $999
    # deposits inside a minute clear a $1,000 limit ten times over. This bounds
    # the SUM over a rolling window, which is the shape real money-movement
    # controls take.
    max_per_window: Optional[float] = None
    window_seconds: int = 3600


# Action types that mutate irreversible state by default.
_DEFAULT_RISKY_ACTIONS = {ActionType.CLICK}  # refined per-step by RiskClass


@dataclass
class Policy:
    """Loaded guardrail configuration."""
    allowed_url_patterns: list[str] = field(default_factory=list)
    blocked_url_patterns: list[str] = field(default_factory=list)
    allowed_action_types: set[str] = field(
        default_factory=lambda: {a.value for a in ActionType})
    # If False, risky/irreversible steps are blocked unless individually approved.
    allow_risky: bool = False
    # Risky steps flagged requires_confirmation become NEEDS_CONFIRMATION.
    require_confirmation_for_risky: bool = True
    # Per-parameter semantic limits, keyed by input parameter name.
    value_rules: dict[str, ValueRule] = field(default_factory=dict)
    # Identities permitted to counter-sign a dual-control run. Empty means the
    # registry is not configured and any independent name is accepted -- fine for
    # a demo, refused in `strict_approvers` mode. Production binds this to the
    # institution's directory; the shape of the check does not change.
    approvers: set[str] = field(default_factory=set)
    strict_approvers: bool = False

    @classmethod
    def from_yaml(cls, path: str) -> "Policy":
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
        return cls(
            allowed_url_patterns=raw.get("allowed_url_patterns", []),
            blocked_url_patterns=raw.get("blocked_url_patterns", []),
            allowed_action_types=set(raw.get("allowed_action_types",
                                             [a.value for a in ActionType])),
            allow_risky=raw.get("allow_risky", False),
            require_confirmation_for_risky=raw.get("require_confirmation_for_risky", True),
            approvers=set(raw.get("approvers") or []),
            strict_approvers=raw.get("strict_approvers", False),
            value_rules={
                name: ValueRule(max=rule.get("max"),
                                dual_control_above=rule.get("dual_control_above"),
                                unit=rule.get("unit", ""),
                                max_per_window=rule.get("max_per_window"),
                                window_seconds=rule.get("window_seconds", 3600))
                for name, rule in (raw.get("value_rules") or {}).items()
            },
        )


class PolicyEngine:
    """Evaluates actions against a Policy (+ optional per-artifact allowlist)."""

    def __init__(self, policy: Policy, artifact_url_patterns: Optional[list[str]] = None,
                 allow_risky_override: Optional[bool] = None):
        self.policy = policy
        self.artifact_url_patterns = artifact_url_patterns or []
        # a run may be launched with explicit approval to perform risky steps
        self.allow_risky = (policy.allow_risky if allow_risky_override is None
                            else allow_risky_override)

    # ---- allowlist -------------------------------------------------------
    @staticmethod
    def _matches_any(url: str, patterns: list[str]) -> bool:
        return any(p in url or fnmatch.fnmatch(url, p) for p in patterns)

    def check_url(self, url: str) -> None:
        if self._matches_any(url, self.policy.blocked_url_patterns):
            raise PolicyViolation(f"URL is explicitly blocked: {url}")
        if self.policy.allowed_url_patterns and not self._matches_any(
                url, self.policy.allowed_url_patterns):
            raise PolicyViolation(f"URL not on global allowlist: {url}")
        if self.artifact_url_patterns and not self._matches_any(
                url, self.artifact_url_patterns):
            raise PolicyViolation(f"URL not on artifact allowlist: {url}")

    def check_action_type(self, action: ActionType) -> None:
        if action.value not in self.policy.allowed_action_types:
            raise PolicyViolation(f"action type not allowed: {action.value}")

    # ---- risk ------------------------------------------------------------
    def evaluate_step(self, step: Step, target_url: Optional[str] = None) -> PolicyDecision:
        """Full pre-flight check for a replay step. Raises on hard block."""
        self.check_action_type(step.action)
        if target_url:
            self.check_url(target_url)
        if step.risk == RiskClass.RISKY:
            if step.requires_confirmation and self.policy.require_confirmation_for_risky:
                return PolicyDecision(Decision.NEEDS_CONFIRMATION,
                                      "irreversible step requires confirmation")
            if not self.allow_risky:
                return PolicyDecision(
                    Decision.BLOCK,
                    "irreversible step blocked; run without --allow-risky approval")
            return PolicyDecision(Decision.ALLOW, "risky step approved for this run")
        return PolicyDecision(Decision.ALLOW)

    # ---- value-level (semantic) policy ----------------------------------
    @staticmethod
    def _as_number(raw) -> Optional[float]:
        """Parse a money-ish input ("$1,500.00", "1500") to a float, else None.

        A parameter we cannot parse is NOT silently allowed: the caller gets an
        explicit refusal, because a limit you cannot evaluate is not a limit."""
        if raw is None:
            return None
        try:
            return float(re.sub(r"[^0-9.\-]", "", str(raw)) or "nan")
        except ValueError:
            return None

    def evaluate_inputs(self, params: dict, ledger=None) -> PolicyDecision:
        """Check a capability's invocation arguments before anything is opened.

        Returns ALLOW, NEEDS_CONFIRMATION (dual control required, naming the
        parameters), or raises PolicyViolation for a hard ceiling breach.

        Velocity is checked against the parameter's TOTAL across capabilities,
        not per capability: two different flows that both move money share one
        budget, because splitting it per flow is the gap an attacker walks
        through.
        """
        needs_dual: list[str] = []
        for name, rule in self.policy.value_rules.items():
            if name not in params:
                continue
            value = self._as_number(params[name])
            if value is None or value != value:            # unparseable / NaN
                raise PolicyViolation(
                    f"value policy: parameter '{name}' is governed by a limit but "
                    f"its value could not be read as a number")
            if rule.max is not None and value > rule.max:
                raise PolicyViolation(
                    f"value policy: '{name}'={value:g}{rule.unit} exceeds the "
                    f"permitted maximum of {rule.max:g}{rule.unit}")
            if rule.max_per_window is not None and ledger is not None:
                spent = ledger.total_in_window(name, rule.window_seconds)
                if spent + value > rule.max_per_window:
                    raise PolicyViolation(
                        f"value policy: '{name}' would take the trailing "
                        f"{rule.window_seconds}s total to "
                        f"{spent + value:g}{rule.unit}, over the permitted "
                        f"{rule.max_per_window:g}{rule.unit} "
                        f"({spent:g}{rule.unit} already spent)")
            if rule.dual_control_above is not None and value > rule.dual_control_above:
                needs_dual.append(name)
        if needs_dual:
            return PolicyDecision(
                Decision.NEEDS_CONFIRMATION,
                "dual control required: " + ", ".join(
                    f"'{n}' exceeds {self.policy.value_rules[n].dual_control_above:g}"
                    f"{self.policy.value_rules[n].unit}" for n in needs_dual),
                params=tuple(needs_dual))
        return PolicyDecision(Decision.ALLOW)

    def approver_is_independent(self, approver: Optional[str],
                                initiator: Optional[str]) -> bool:
        """Dual control means two *different*, *authorised* people.

        Two conditions, and they are not the same one. Independence stops a run
        approving itself. Registry membership stops any typed string counting as
        a second pair of eyes -- without it, `--approver whoever` is theatre. The
        registry stands in for the institution's directory; binding it to real
        authenticated identity changes where the set comes from, not the check.
        """
        if not approver:
            return False
        approver = approver.strip()
        if approver.lower() == (initiator or "").strip().lower():
            return False
        if self.policy.approvers:
            return approver.lower() in {a.lower() for a in self.policy.approvers}
        # registry not configured: independence alone, unless strictness is asked
        return not self.policy.strict_approvers

    # ---- discovery-time guard -------------------------------------------
    def evaluate_discovery_action(self, action_type: ActionType, url: str,
                                  risk: RiskClass) -> PolicyDecision:
        """Guard used by the discovery loop before executing a model action."""
        self.check_action_type(action_type)
        self.check_url(url)
        if risk == RiskClass.RISKY and not self.allow_risky:
            return PolicyDecision(
                Decision.NEEDS_CONFIRMATION,
                "model proposed an irreversible action during discovery")
        return PolicyDecision(Decision.ALLOW)
