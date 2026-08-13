"""
Cross-tenant reuse: bind one artifact to another institution running the same
vendor product, with minimal per-tenant overrides.

Hundreds of tenants run the same vendor product, branded and versioned
differently. The *flow* and page *structure* are shared; what differs is the
binding (base URL, tenant id) and a handful of visible strings ("Member ID" vs.
"Member Number", "Search" vs. "Find"). Rather than re-record per tenant, we:

  * keep ONE artifact (recorded on a base tenant), and
  * apply a small `TenantOverride` -- a base_url + a string `label_map` -- that
    re-points the binding and remaps locator/checkpoint strings.

`apply_overrides` returns a new, tenant-bound artifact. Locators that were left
semantic (role+name, label-proximity) are exactly the ones the label_map fixes;
structural fallbacks mean an un-mapped tenant still often works (with a drift
signal) rather than breaking outright -- graceful degradation, not a cliff.

`canonicalize_url` normalises concrete routes to patterns (/member?mid=12345 ->
/member?mid=:id) for cross-tenant matching and drift reporting.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from pydantic import BaseModel, Field

from .schema import CapabilityArtifact


class TenantOverride(BaseModel):
    tenant_id: str
    base_url: Optional[str] = None
    entry_path: Optional[str] = None
    version: Optional[str] = None
    label_map: dict[str, str] = Field(
        default_factory=dict,
        description="Per-tenant string remaps applied to locator candidate values "
        "and checkpoint text, e.g. {'Member ID': 'Member Number'}.",
    )

    @classmethod
    def load(cls, path: str) -> "TenantOverride":
        with open(path) as f:
            return cls.model_validate(json.load(f))


def _remap(text: str, label_map: dict[str, str]) -> str:
    if not text:
        return text
    out = text
    # longest keys first so overlapping labels remap deterministically
    for k in sorted(label_map, key=len, reverse=True):
        out = out.replace(k, label_map[k])
    return out


def apply_overrides(art: CapabilityArtifact,
                    ov: TenantOverride) -> CapabilityArtifact:
    """Return a new artifact rebound to a tenant with string overrides applied."""
    a = art.model_copy(deep=True)
    a.target.tenant_id = ov.tenant_id
    if ov.version:
        a.target.version = ov.version
    if ov.entry_path:
        a.target.entry_path = ov.entry_path
    if ov.base_url:
        base = ov.base_url.rstrip("/")
        a.target.base_url = base
        a.target.allowed_url_patterns = [f"{base}/*", base]

    lm = ov.label_map
    if lm:
        def fix_locator(loc):
            for c in loc.candidates:
                c.value = _remap(c.value, lm)

        for step in a.steps:
            if step.target:
                fix_locator(step.target)
            if step.checkpoint:
                step.checkpoint.value = _remap(step.checkpoint.value, lm)
            if step.extract:
                fix_locator(step.extract.locator)
        a.success.value = _remap(a.success.value, lm)
    return a


_ID_SEG = re.compile(r"/\d+")
_ID_QS = re.compile(r"(=)\d+")


def canonicalize_url(path: str) -> str:
    """Normalise concrete ids to a pattern: /member?mid=12345 -> /member?mid=:id."""
    out = _ID_SEG.sub("/:id", path)
    out = _ID_QS.sub(r"\1:id", out)
    return out
