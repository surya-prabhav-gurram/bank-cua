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
        return CapabilityArtifact.from_json(open(self._path(cap_id)).read())

    def list(self) -> list[CapabilityArtifact]:
        out = []
        for p in sorted(glob.glob(os.path.join(self.root, "*.json"))):
            try:
                out.append(CapabilityArtifact.from_json(open(p).read()))
            except Exception:
                pass
        return out

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
