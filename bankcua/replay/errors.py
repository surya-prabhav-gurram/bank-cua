"""Value transforms applied to extracted outputs during replay."""
from __future__ import annotations

import re
from typing import Optional


def apply_transform(value: str, transform: Optional[str]):
    if value is None:
        return value
    if transform is None:
        return value
    if transform == "strip":
        return value.strip()
    if transform == "digits_only":
        return int(re.sub(r"\D", "", value) or "0")
    if transform == "money_to_cents":
        cleaned = re.sub(r"[^0-9.]", "", value)
        if not cleaned:
            return 0
        return round(float(cleaned) * 100)
    return value
