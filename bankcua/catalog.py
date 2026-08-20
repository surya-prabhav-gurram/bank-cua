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
                    note: str = "", requires_confirmation: bool | None = None
                    ) -> CapabilityArtifact:
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
        if requires_confirmation is not None:
            # A reviewer may separate "this is irreversible" from "a person must
            # be present each time". They are different questions: a transfer is
            # permanently irreversible, but it is also bounded by an amount
            # ceiling, a velocity budget and dual control, so an explicitly
            # approved run can perform it without stopping for someone. Applying
            # a hold has no such envelope, so it keeps its per-run confirmation.
            #
            # Only a HUMAN may make this call -- it is reachable through review
            # and nowhere else -- and the reason is recorded on the step, because
            # this is the single edit that turns a capability from
            # always-escalates into may-run-unattended.
            step.requires_confirmation = requires_confirmation
        step.risk_reviewed = True
        if note:
            step.risk_reason = (f"{step.risk_reason} | reviewed: {note}"
                                if step.risk_reason else f"reviewed: {note}")
        self.save(art)
        return art

    def refresh_conditions(self, cap_id: str) -> tuple["CapabilityArtifact", dict]:
        """Re-attach the vendor condition library, as a reviewable version bump.

        The taxonomy is copied INTO an artifact when it is compiled, not read at
        replay time. That is deliberate: an approved capability's behaviour must
        not change because someone edited a shared file, since the approval was
        granted against specific behaviour and nobody re-reviewed it. The cost is
        that a genuine improvement to the library -- a detector corrected after
        it was found to match a warning banner rather than a denial -- does not
        reach artifacts already recorded.

        This is the governed path for that: it re-attaches the library, bumps the
        version, and lands the result in `draft`, so the existing approval gate
        decides whether the new behaviour may run unattended. Same shape as
        drift repair, and for the same reason -- propagate, but never silently.
        """
        from .knowledge import conditions_for
        from .schema import ApprovalState

        art = self.get(cap_id)
        before = {c.code for c in art.known_conditions}
        fresh = conditions_for(art.target.vendor_product)
        if not fresh:
            raise ValueError(
                f"no condition library for vendor "
                f"{art.target.vendor_product!r}; nothing to refresh")
        after = {c.code for c in fresh}
        changed = [c.code for c in fresh
                   if c.model_dump() not in [o.model_dump()
                                             for o in art.known_conditions]]
        art.known_conditions = fresh
        parts = (art.version or "1.0.0").split(".")
        while len(parts) < 3:
            parts.append("0")
        parts[2] = str(int(parts[2]) + 1) if parts[2].isdigit() else "1"
        art.version = ".".join(parts[:3])
        art.approval_state = ApprovalState.DRAFT
        art.stability = None
        self.save(art)
        return art, {"added": sorted(after - before),
                     "removed": sorted(before - after),
                     "modified": sorted(set(changed) & before)}

    def manifest(self, supplied_by_service: set[str] | None = None) -> list[dict]:
        """Function-calling manifest an agent can discover and invoke from.

        Two classes of input are deliberately ABSENT from what an agent sees:

          * `sensitive` inputs, always. Publishing "this capability takes a
            password" in an agent-facing tool list is an invitation to send one,
            and no caller should ever hold one -- the service resolves secrets
            from an operator alias. A live model given the unfiltered manifest
            stopped and asked the user for the operator password rather than
            calling the tool, which is the manifest working exactly as written
            and exactly wrong.
          * anything the SERVICE supplies from the operator's identity, such as
            the branch code. A caller cannot know it and must not choose it.

        What remains is what the caller genuinely has to decide.
        """
        hidden = set(supplied_by_service or set())
        tools = []
        for a in self.list():
            hide = hidden | a.secret_params()
            props, required = {}, []
            for p in a.inputs:
                if p.name in hide:
                    continue
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
