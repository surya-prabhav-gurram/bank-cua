"""
Per-vendor runtime-condition libraries, loaded from data.

Why this is a loader and not a Python literal
---------------------------------------------
How a vendor product signals "locked account" or "session gone" is a property of
THAT PRODUCT -- not of one recorded flow, and not of one institution. Curating it
once per vendor and inheriting it into every artifact for that vendor is the
cross-tenant reuse the brief asks for: one place to maintain, one place to review.

The first version held these as Python literals in this module. That was rejected
once the audience became clear. The people best placed to say "Meridian signals a
locked share like THIS" are the people who know the vendor's screens, not the
people who know our replay engine -- and a taxonomy they cannot read is a taxonomy
they cannot correct. Holding it as YAML also means adapting to a new vendor is a
file, not a patch: the whole Meridian error taxonomy landed without recompiling
anything, which is the load-bearing claim of the adaptation.

The seam: swap `config/knowledge/<vendor>.yaml` and every artifact for that vendor
inherits the new taxonomy, without touching the replay engine, the schema, or any
recorded capability.

Validation is Pydantic's, not ours: each entry is a `KnownCondition`, so a
malformed detector or an unknown `klass` fails at load with a real error rather
than silently degrading into a condition that never fires -- which is the failure
mode that matters, because a detector that never matches is invisible.
"""
from __future__ import annotations

import functools
import os
from typing import Optional

import yaml

from .schema import KnownCondition

#: Where vendor libraries live. Overridable so tests can point at a fixture
#: directory without mutating the repo's real taxonomy.
KNOWLEDGE_DIR = os.environ.get(
    "BANKCUA_KNOWLEDGE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "config", "knowledge"),
)


class VendorLibraryError(ValueError):
    """A vendor library exists but could not be read as a condition set.

    Raised rather than returning an empty list: a vendor whose taxonomy failed to
    load would replay with NO condition detection at all, turning every business
    outcome into an unexplained checkpoint failure. Failing loudly at load beats
    discovering that on a member's account.
    """


@functools.lru_cache(maxsize=None)
def _load(vendor_key: str) -> tuple[KnownCondition, ...]:
    path = os.path.join(KNOWLEDGE_DIR, f"{vendor_key}.yaml")
    if not os.path.exists(path):
        return ()
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    try:
        return tuple(KnownCondition.model_validate(c)
                     for c in (raw.get("conditions") or []))
    except Exception as ex:
        raise VendorLibraryError(f"{path}: {ex}") from ex


def available_vendors() -> list[str]:
    """Vendor keys with a library on disk, for diagnostics and tests."""
    if not os.path.isdir(KNOWLEDGE_DIR):
        return []
    return sorted(n[:-5] for n in os.listdir(KNOWLEDGE_DIR)
                  if n.endswith(".yaml"))


def conditions_for(vendor_product: Optional[str]) -> list[KnownCondition]:
    """Conditions for a vendor, as fresh objects the caller may mutate.

    Deep-copied on the way out because the cached tuple is shared by every
    artifact for the vendor: a tenant override that remaps detector text must not
    reach back and edit the library other tenants are inheriting.
    """
    if not vendor_product:
        return []
    return [KnownCondition.model_validate(c.model_dump())
            for c in _load(vendor_product.strip().lower())]
