"""
Will this artifact run on that surface?

A capability recorded on one surface carries locator strategies that another
surface may not be able to honour -- a DOM-less driver cannot resolve CSS or
XPath, however well-formed. Because every step stores an ORDERED list of
candidates, the answer is decidable statically: a step is portable if at least
one of its candidates uses a strategy the target surface declares support for.

This is a static check on purpose. Finding out that step 7 is unreachable after
steps 1-6 have already been performed is how automation half-completes an
irreversible flow. Ask before launching.
"""
from __future__ import annotations

from typing import Iterable, Optional

from pydantic import BaseModel, Field

from .schema import CapabilityArtifact, Locator, LocatorKind


class StepPortability(BaseModel):
    step_index: int
    intent: str
    what: str = Field(description="Which locator on the step: target or extract.")
    honoured: list[str] = Field(default_factory=list)
    unhonoured: list[str] = Field(default_factory=list)

    @property
    def portable(self) -> bool:
        return bool(self.honoured)


class PortabilityReport(BaseModel):
    capability_id: str
    surface: str
    portable: bool
    steps: list[StepPortability] = Field(default_factory=list)

    def blockers(self) -> list[StepPortability]:
        return [s for s in self.steps if not s.portable]

    def summary(self) -> str:
        if self.portable:
            return (f"{self.capability_id} is portable to '{self.surface}': every "
                    f"targeted step has a strategy this surface can honour.")
        bad = self.blockers()
        lines = [f"{self.capability_id} is NOT portable to '{self.surface}': "
                 f"{len(bad)} step(s) have no honourable strategy."]
        for s in bad:
            lines.append(f"  step {s.step_index} ({s.what}) '{s.intent[:50]}' "
                         f"-- offers only {s.unhonoured}")
        return "\n".join(lines)


def _classify(loc: Locator, supported: Iterable[LocatorKind]) -> tuple[list, list]:
    ok, no = [], []
    for c in loc.candidates:
        (ok if c.kind in supported else no).append(c.kind.value)
    return ok, no


def portability_report(art: CapabilityArtifact, surface_cls,
                       surface_name: Optional[str] = None) -> PortabilityReport:
    supported = set(getattr(surface_cls, "supported_locator_kinds", set()))
    name = surface_name or getattr(surface_cls, "__name__", "surface")
    steps: list[StepPortability] = []
    for st in art.steps:
        for what, loc in (("target", st.target),
                          ("extract", st.extract.locator if st.extract else None)):
            if loc is None:
                continue
            ok, no = _classify(loc, supported)
            steps.append(StepPortability(step_index=st.index, intent=st.intent,
                                         what=what, honoured=ok, unhonoured=no))
    return PortabilityReport(capability_id=art.id, surface=name,
                             portable=all(s.portable for s in steps), steps=steps)
