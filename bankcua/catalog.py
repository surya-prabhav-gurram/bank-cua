"""
Capability catalog: expose saved artifacts as callable-by-name capabilities.

This is the agent-facing surface. A calling AI agent lists capabilities,
reads each one's typed contract (inputs/outputs/description) as a
function-calling manifest, and invokes by id with typed args -- without ever
seeing the steps or re-reasoning about the UI.
"""
from __future__ import annotations

import glob
import os

from .schema import CapabilityArtifact


class Catalog:
    def __init__(self, root: str = "capabilities"):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, cap_id: str) -> str:
        return os.path.join(self.root, f"{cap_id}.json")

    def save(self, art: CapabilityArtifact) -> str:
        p = self._path(art.id)
        with open(p, "w") as f:
            f.write(art.to_json())
        return p

    def get(self, cap_id: str) -> CapabilityArtifact:
        with open(self._path(cap_id)) as f:
            return CapabilityArtifact.from_json(f.read())

    def list(self) -> list[CapabilityArtifact]:
        out = []
        for p in sorted(glob.glob(os.path.join(self.root, "*.json"))):
            try:
                with open(p) as f:
                    out.append(CapabilityArtifact.from_json(f.read()))
            except Exception:
                pass
        return out

    # ---- approval gate ---------------------------------------------------
    @staticmethod
    def unreviewed_risky_steps(art: CapabilityArtifact) -> list[int]:
        """Risky steps a human has not ratified.

        Risk classification is a heuristic (see agent.loop.classify_risk). A
        heuristic may promote a capability to draft, but it may not promote it to
        approved: approval is the point where unattended replay of an
        irreversible action becomes possible, so a person has to have looked at
        every risky step first."""
        return [st.index for st in art.steps
                if st.risk.value == "risky" and not st.risk_reviewed]

    def approve(self, cap_id: str) -> CapabilityArtifact:
        """Promote draft -> approved. Refuses while any risky step is unreviewed."""
        from .schema import ApprovalState
        art = self.get(cap_id)
        pending = self.unreviewed_risky_steps(art)
        if pending:
            raise ValueError(
                f"cannot approve '{cap_id}': risky steps {pending} have not been "
                f"reviewed. Run `catalog review --id {cap_id} --step N "
                f"--risk safe|risky --note ...` for each.")
        art.approval_state = ApprovalState.APPROVED
        self.save(art)
        return art

    def review_step(self, cap_id: str, index: int, risk: str | None = None,
                    note: str = "") -> CapabilityArtifact:
        """Record a human's ratification of one step's risk classification.

        The reviewer may also *change* the class -- that is the point of a review
        gate rather than a checkbox. An over-eager structural signal being
        downgraded (with a reason) is the system working, not failing."""
        from .schema import RiskClass
        art = self.get(cap_id)
        step = next((st for st in art.steps if st.index == index), None)
        if step is None:
            raise ValueError(f"no step {index} in '{cap_id}'")
        if risk is not None:
            step.risk = RiskClass(risk)
            step.requires_confirmation = (step.risk == RiskClass.RISKY)
        step.risk_reviewed = True
        if note:
            step.risk_reason = (f"{step.risk_reason} | reviewed: {note}"
                                if step.risk_reason else f"reviewed: {note}")
        self.save(art)
        return art

    def manifest(self) -> list[dict]:
        """Function-calling style manifest an agent can discover + invoke from."""
        tools = []
        for a in self.list():
            props, required = {}, []
            for p in a.inputs:
                props[p.name] = {"type": _json_type(p.type.value),
                                 "description": p.description}
                if p.required:
                    required.append(p.name)
            tools.append({
                "name": a.id,
                "version": a.version,
                "approval_state": a.approval_state.value,
                "description": a.description,
                "input_schema": {"type": "object", "properties": props,
                                 "required": required},
                "returns": {o.name: o.type.value for o in a.outputs},
            })
        return tools


def _json_type(t: str) -> str:
    return {"integer": "integer", "number": "number", "boolean": "boolean"}.get(
        t, "string")
