"""
Drift-driven artifact repair: detect → propose → a human approves → apply.

Replay already emits a `DriftSignal` whenever a step resolves on a non-primary
locator candidate. One drift is noise -- a slow render, a one-off. A step that
drifts to the same fallback on run after run is not noise: it is the primary
strategy having gone stale, and it is the last warning before that fallback goes
too and the capability fails outright.

The loop closed here is deliberately NOT self-modifying. Automation that silently
rewrites its own instructions for driving a bank is a worse problem than the
staleness it fixes. So repair *proposes*: it emits a reviewable diff, bumps the
version, and lands the result in `draft`, which the approval gate already refuses
to run unattended. A person still says yes -- they just no longer have to notice.

What is repairable, and what is only reportable
-----------------------------------------------
* Reorderable -- the artifact already contains a candidate that keeps working.
  Promoting it to primary is a safe, mechanical, reversible edit, and it is the
  common case for structural churn.
* Reportable -- every candidate for a step has failed, or the drift is a per-tenant
  rename where the correct new string is not derivable from a failure. The
  proposal says so plainly instead of guessing; a wrong guess here silently
  targets the wrong control.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict
from typing import Optional

from pydantic import BaseModel, Field

from .schema import CapabilityArtifact, LocatorKind

# Strategies that describe WHAT a control is. They survive theming, translation
# and per-tenant branding, which is why locator synthesis ranks them first.
_SEMANTIC = {LocatorKind.ROLE, LocatorKind.NEAR_LABEL, LocatorKind.LABEL,
             LocatorKind.PLACEHOLDER, LocatorKind.TEXT, LocatorKind.ALT_TEXT,
             LocatorKind.TITLE, LocatorKind.TEST_ID}


class DriftObservation(BaseModel):
    ts: float
    capability_id: str
    version: str
    tenant_id: Optional[str] = None
    step_index: int
    candidate_index: int
    kind: str = ""
    description: str = ""


class DriftLedger:
    """Append-only record of drift across runs. Repair needs history; a single
    ReplayResult cannot tell a blip from a trend."""

    def __init__(self, path: str = "evidence/drift_ledger.jsonl"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def record_result(self, result, tenant_id: Optional[str] = None) -> int:
        rows = [DriftObservation(ts=time.time(), capability_id=result.capability_id,
                                 version=result.version, tenant_id=tenant_id,
                                 step_index=d.step_index,
                                 candidate_index=d.candidate_index,
                                 kind=d.kind, description=d.description)
                for d in result.drifts]
        if rows:
            with open(self.path, "a") as f:
                for r in rows:
                    f.write(r.model_dump_json() + "\n")
        return len(rows)

    def observations(self, capability_id: Optional[str] = None,
                     tenant_id: Optional[str] = None) -> list[DriftObservation]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = DriftObservation.model_validate_json(line)
                except Exception:
                    continue
                if capability_id and o.capability_id != capability_id:
                    continue
                if tenant_id is not None and o.tenant_id != tenant_id:
                    continue
                out.append(o)
        return out

class StepRepair(BaseModel):
    step_index: int
    description: str
    from_candidate: int = 0
    to_candidate: int
    to_kind: str = ""
    occurrences: int
    rationale: str


class RepairProposal(BaseModel):
    id: str
    capability_id: str
    from_version: str
    to_version: str
    tenant_id: Optional[str] = None
    created_at: float = 0.0
    repairs: list[StepRepair] = Field(default_factory=list)
    unrepairable: list[str] = Field(default_factory=list)
    applied: bool = False

    def summary(self) -> str:
        head = (f"{self.id}: {self.capability_id} {self.from_version} -> "
                f"{self.to_version}"
                + (f" (tenant {self.tenant_id})" if self.tenant_id else ""))
        lines = [head]
        for r in self.repairs:
            lines.append(f"  step {r.step_index} '{r.description[:44]}': promote "
                         f"candidate {r.to_candidate} ({r.to_kind}) to primary "
                         f"-- {r.rationale}")
        for u in self.unrepairable:
            lines.append(f"  NEEDS A HUMAN: {u}")
        if not self.repairs and not self.unrepairable:
            lines.append("  no drift above threshold; nothing to propose")
        return "\n".join(lines)


def _bump_minor(version: str) -> str:
    parts = (version or "1.0.0").split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        parts[2] = str(int(parts[2]) + 1)
    except ValueError:
        parts[2] = "1"
    return ".".join(parts[:3])


def analyse(art: CapabilityArtifact, ledger: DriftLedger,
            min_occurrences: int = 3, tenant_id: Optional[str] = None
            ) -> RepairProposal:
    """Turn a drift history into a reviewable proposal."""
    obs = ledger.observations(art.id, tenant_id)
    by_step: dict[int, list[DriftObservation]] = defaultdict(list)
    for o in obs:
        by_step[o.step_index].append(o)

    repairs, unrepairable = [], []
    for step_index, rows in sorted(by_step.items()):
        counts = defaultdict(int)
        for r in rows:
            counts[r.candidate_index] += 1
        winner, n = max(counts.items(), key=lambda kv: kv[1])
        if n < min_occurrences:
            continue
        step = next((s for s in art.steps if s.index == step_index), None)
        loc = step.target if step and step.target else (
            step.extract.locator if step and step.extract else None)
        if loc is None or winner >= len(loc.candidates):
            unrepairable.append(
                f"step {step_index} drifted {n}x but its locator no longer has "
                f"candidate {winner} -- re-record or supply a tenant override")
            continue
        cand = loc.candidates[winner]
        primary = loc.candidates[0]
        # The judgement that stops this loop making things worse. Drift from a
        # SEMANTIC primary to a STRUCTURAL fallback is not a stale locator -- it is
        # a renamed control, and the repair is a per-tenant string override.
        # Promoting the CSS path would "fix" the symptom by permanently trading a
        # strategy that survives rebranding for one that breaks on the next layout
        # change. Repair proposes only what improves the artifact.
        if primary.kind in _SEMANTIC and cand.kind not in _SEMANTIC:
            unrepairable.append(
                f"step {step_index} ({loc.description[:40]}) fell back to "
                f"{cand.kind.value} in {n} runs while its semantic primary "
                f"({primary.kind.value} {primary.value!r}) stopped matching. That "
                f"signature is a RENAMED control, not a stale locator: supply a "
                f"tenant label_map for {primary.value!r} rather than demoting this "
                f"step to a structural path")
            continue
        repairs.append(StepRepair(
            step_index=step_index, description=loc.description,
            to_candidate=winner, to_kind=cand.kind.value, occurrences=n,
            rationale=(f"resolved on candidate {winner} in {n} runs; the primary "
                       f"has stopped matching")))

    return RepairProposal(
        id=f"repair-{art.id}-{art.version}" + (f"-{tenant_id}" if tenant_id else ""),
        capability_id=art.id, from_version=art.version,
        to_version=_bump_minor(art.version), tenant_id=tenant_id,
        created_at=time.time(), repairs=repairs, unrepairable=unrepairable)


def apply(art: CapabilityArtifact, proposal: RepairProposal) -> CapabilityArtifact:
    """Return a NEW artifact version with the proposed promotions applied.

    The result is `draft` by construction, so the existing approval gate -- which
    already refuses unattended replay of an unapproved capability, and refuses to
    approve one with an unreviewed risky step -- is what lets it back into
    production. Repair borrows the governance rather than routing around it.
    """
    from .schema import ApprovalState

    a = art.model_copy(deep=True)
    for r in proposal.repairs:
        step = next((s for s in a.steps if s.index == r.step_index), None)
        if step is None:
            continue
        loc = step.target if step.target else (
            step.extract.locator if step.extract else None)
        if loc is None or r.to_candidate >= len(loc.candidates):
            continue
        promoted = loc.candidates.pop(r.to_candidate)
        promoted.reasoning = (f"{promoted.reasoning} | promoted by drift repair "
                              f"{proposal.id}: matched in {r.occurrences} runs "
                              f"where the previous primary did not.")
        loc.candidates.insert(0, promoted)
    a.version = proposal.to_version
    a.approval_state = ApprovalState.DRAFT
    a.stability = None          # a changed flow has not earned the old score
    return a


class ProposalStore:
    def __init__(self, root: str = "evidence/repairs"):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, pid: str) -> str:
        return os.path.join(self.root, f"{pid}.json")

    def save(self, p: RepairProposal) -> str:
        with open(self._path(p.id), "w") as f:
            f.write(p.model_dump_json(indent=2))
        return self._path(p.id)

    def load(self, pid: str) -> RepairProposal:
        with open(self._path(pid)) as f:
            return RepairProposal.model_validate_json(f.read())

    def list(self) -> list[RepairProposal]:
        out = []
        for name in sorted(os.listdir(self.root)):
            if name.endswith(".json"):
                try:
                    out.append(self.load(name[:-5]))
                except Exception:
                    continue
        return out
