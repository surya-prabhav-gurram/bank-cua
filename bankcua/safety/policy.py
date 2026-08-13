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

  3. It is advisory-free: a violation raises, it does not warn-and-continue.

Why this shape: in a bank, "accidentally created a sub-account" is far worse
than "refused to create one." Failing closed on the irreversible class is the
correct default; the write-up (section 6) covers the limits.
"""
from __future__ import annotations

import fnmatch
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
        for p in patterns:
            if p in url or fnmatch.fnmatch(url, p):
                return True
        return False

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
