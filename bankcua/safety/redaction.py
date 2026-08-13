"""
Redaction of secrets and regulated data.

Rule: raw sensitive values NEVER reach an artifact, a log, or a stored
observation. Two mechanisms:

  1. Name-based: any input parameter declared `sensitive` (credentials, member
     PII) is redacted by name wherever its value could appear.
  2. Pattern-based: a safety net of regexes catches SSNs, card numbers, and long
     digit runs that slip into free text (page snapshots, model output) even if
     we didn't know the field name.

Screenshots can still contain PII on screen; we treat them as sensitive
evidence (see observability + REPORT section 6 for the limitation and the
production answer: masked capture / restricted evidence store).
"""
from __future__ import annotations

import re
from typing import Any, Iterable

REDACTED = "***REDACTED***"

# Pattern-based safety net (order matters; most specific first).
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "***SSN***"),
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "***CARD***"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "***EMAIL***"),
]


def redact_text(text: str, extra_literals: Iterable[str] = ()) -> str:
    """Redact known literals (e.g. a password value we were given) + PII patterns."""
    if not text:
        return text
    out = text
    for lit in extra_literals:
        if lit:
            out = out.replace(lit, REDACTED)
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


def redact_value_by_name(name: str, value: Any, secret_names: set[str]) -> Any:
    if name in secret_names:
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_mapping(data: dict[str, Any], secret_names: set[str]) -> dict[str, Any]:
    """Return a copy with sensitive keys masked and free-text PII scrubbed."""
    clean: dict[str, Any] = {}
    for k, v in data.items():
        if k in secret_names:
            clean[k] = REDACTED
        elif isinstance(v, dict):
            clean[k] = redact_mapping(v, secret_names)
        elif isinstance(v, list):
            clean[k] = [redact_mapping(x, secret_names) if isinstance(x, dict)
                        else (redact_text(x) if isinstance(x, str) else x) for x in v]
        elif isinstance(v, str):
            clean[k] = redact_text(v)
        else:
            clean[k] = v
    return clean
