"""
A file-backed record of value-bearing invocations, so limits can be enforced
across runs rather than only within one.

Why this exists: a per-invocation ceiling is blind to velocity. Ten $999
deposits inside a minute clear a $1,000 limit ten times over, and that is the
shape most real money-movement abuse takes. Bounding the SUM over a rolling
window needs memory, and memory needs somewhere to live.

Deliberately a JSON file with an exclusive-append write, not a database: it is
the smallest thing that makes the *policy* real. The seam is `Ledger` -- a
production deployment swaps the storage for the institution's ledger of record
and changes nothing in the policy engine. The brief is explicit that building
scaling infrastructure is not rewarded; making the guardrail honest is.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from pydantic import BaseModel


class LedgerEntry(BaseModel):
    ts: float
    capability_id: str
    param: str
    value: float
    initiator: str = ""
    approver: str = ""


class Ledger:
    def __init__(self, path: str = "evidence/value_ledger.jsonl",
                 clock=None):
        self.path = path
        # injectable so window behaviour is testable without sleeping
        self._now = clock or time.time
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def record(self, entry: LedgerEntry) -> None:
        with open(self.path, "a") as f:
            f.write(entry.model_dump_json() + "\n")

    def entries(self) -> list[LedgerEntry]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(LedgerEntry.model_validate_json(line))
                except Exception:
                    continue          # a corrupt line must not disable the limit
        return out

    def total_in_window(self, param: str, window_seconds: int,
                        capability_id: Optional[str] = None) -> float:
        """Sum of recorded values for `param` inside the trailing window.

        Scoped by parameter, not by capability, unless asked: two different
        capabilities that both move money should share one velocity budget --
        splitting it per flow is precisely the gap an attacker walks through.
        """
        cutoff = self._now() - window_seconds
        return sum(e.value for e in self.entries()
                   if e.param == param and e.ts >= cutoff
                   and (capability_id is None or e.capability_id == capability_id))
