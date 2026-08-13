"""
The replay result contract.

The single most important distinction (per the brief): a "no such member" is a
legitimate BUSINESS_OUTCOME the caller needs, NOT a crash. The contract makes
that explicit and separate from hard FAILUREs, and records recoverable
conditions that were handled in-band along the way.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ReplayStatus(str, Enum):
    SUCCESS = "success"                 # goal reached; outputs populated
    BUSINESS_OUTCOME = "business_outcome"  # legitimate non-success the caller wants
    ESCALATED = "escalated"            # paused for a human (e.g. risky confirmation)
    FAILURE = "failure"                # hard failure; debuggable error


class RecoveryEvent(BaseModel):
    step_index: int
    condition_code: str
    action: str
    attempts: int
    succeeded: bool


class DriftSignal(BaseModel):
    """A step whose primary locator failed and a fallback candidate resolved.
    A rising drift rate across runs is the early-warning signal for per-tenant/
    version UI drift (see REPORT section 4)."""
    step_index: int
    description: str
    candidate_index: int          # 0 = primary; >0 = a fallback was needed
    kind: str = ""


class AssistEvent(BaseModel):
    """A bounded, policy-checked single-step LLM recovery that was applied."""
    step_index: int
    action: str
    intent: str = ""
    succeeded: bool = False


class FailureDetail(BaseModel):
    code: str
    step_index: Optional[int] = None
    expected: str = ""
    observed: str = ""
    evidence: dict[str, str] = Field(default_factory=dict)


class BusinessOutcome(BaseModel):
    code: str
    message: str = ""
    step_index: Optional[int] = None


class ReplayResult(BaseModel):
    status: ReplayStatus
    capability_id: str
    version: str = ""
    outputs: dict[str, Any] = Field(default_factory=dict)
    business_outcome: Optional[BusinessOutcome] = None
    failure: Optional[FailureDetail] = None
    recoveries: list[RecoveryEvent] = Field(default_factory=list)
    drifts: list[DriftSignal] = Field(default_factory=list)
    assists: list[AssistEvent] = Field(default_factory=list)
    intervention_id: Optional[str] = None
    steps_executed: int = 0
    duration_s: float = 0.0

    def ok(self) -> bool:
        return self.status == ReplayStatus.SUCCESS
