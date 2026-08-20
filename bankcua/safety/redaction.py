"""
Redaction of secrets and regulated data.

Rule: raw sensitive values NEVER reach an artifact, a log, or a stored
observation. Two mechanisms:

  1. Name-based: any input parameter declared `sensitive` (credentials, member
     PII) is redacted by name wherever its value could appear.
  2. Pattern-based: a safety net of regexes catches SSNs, card numbers, and
     emails that slip into free text (page snapshots, model output) even if we
     didn't know the field name. Card detection is Luhn-checked so it does not
     fire on ordinary digit runs such as timestamps or run ids.

Literal matching is word-boundary anchored, and the mapping form of
`extra_literals` names the parameter in the placeholder, so an over-redaction
(a secret whose value is also a common word) reads as intentional rather than
as corrupted evidence. Over-redaction remains the deliberate bias.

Screenshots can still contain PII on screen; we treat them as sensitive
evidence (see observability + REPORT section 6 for the limitation and the
production answer: masked capture / restricted evidence store).
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Union

REDACTED = "***REDACTED***"

# Minimum literal length we are willing to substitute. Shorter values are too
# ambiguous -- redacting every "12" in a log destroys more than it protects.
_MIN_LITERAL_LEN = 3


def _luhn(digits: str) -> bool:
    """Luhn checksum, used to keep the card pattern from firing on ordinary
    digit runs (timestamps, run ids, account numbers). Real PANs satisfy Luhn;
    an arbitrary 14-digit string satisfies it ~10% of the time, which turns a
    noisy pattern into a usable one."""
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


# Candidate PAN: 13-19 digits, optional single space/dash separators, not glued
# to another digit or dash (so `run-20260813-183533` is not a candidate at all).
_CARD_CANDIDATE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")


def _card_sub(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m.group(0))
    if 13 <= len(digits) <= 19 and _luhn(digits):
        return "***CARD***"
    return m.group(0)          # not a card -- leave the text intact


# Pattern-based safety net (order matters; most specific first).
_PATTERNS: list[tuple[re.Pattern, Union[str, object]]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "***SSN***"),
    (_CARD_CANDIDATE, _card_sub),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "***EMAIL***"),
]


def _literal_pattern(lit: str) -> re.Pattern:
    """Word-boundary-ish match so a literal can only replace a whole token."""
    return re.compile(rf"(?<!\w){re.escape(lit)}(?!\w)")


def redact_text(text: str,
                extra_literals: Union[Iterable[str], Mapping[str, str]] = ()) -> str:
    """Redact known literals (e.g. a password value we were given) + PII patterns.

    `extra_literals` may be a plain iterable of values, or a mapping of
    param-name -> value. The mapping form names the parameter in the
    placeholder (``***REDACTED:username***``). That matters when a secret value
    is also an ordinary word: redacting it is still the correct, conservative
    behaviour, but the evidence stays legible to a reviewer instead of reading
    like corrupted output.
    """
    if not text:
        return text
    out = text
    items = (extra_literals.items() if isinstance(extra_literals, Mapping)
             else ((None, lit) for lit in extra_literals))
    for name, lit in items:
        if not lit:
            continue
        lit = str(lit)
        if len(lit) < _MIN_LITERAL_LEN:
            continue
        out = _literal_pattern(lit).sub(
            f"***REDACTED:{name}***" if name else REDACTED, out)
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    return out


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
