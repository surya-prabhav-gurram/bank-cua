"""
Discovery task definition: the input to a discovery run.

Separates the *goal + contract we want* (capability id, params, desired outputs,
success condition) from the *binding* (base_url, vendor/version). The compiler
turns a successful run of this task into a CapabilityArtifact with the same
contract.
"""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from ..schema import Checkpoint, InputParameter, OutputField, SurfaceType


class DiscoveryTask(BaseModel):
    capability_id: str
    name: str
    description: str
    goal: str = Field(description="Natural-language goal; may reference {param}s.")

    app_id: str
    surface_type: SurfaceType = SurfaceType.WEB
    base_url: str
    entry_path: str = "/"
    tenant_id: str | None = None
    vendor_product: str | None = None
    version: str | None = None

    inputs: list[InputParameter] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    success: Checkpoint

    max_steps: int = 20
    max_seconds: float = 300.0     # wall-clock stopping condition

    # Values supplied for THIS discovery run (secrets never persisted).
    param_values: dict[str, Any] = Field(default_factory=dict)

    def rendered_goal(self) -> str:
        try:
            return self.goal.format(**self.param_values)
        except Exception:
            return self.goal

    @classmethod
    def load(cls, path: str) -> "DiscoveryTask":
        with open(path) as f:
            return cls.model_validate(json.load(f))
