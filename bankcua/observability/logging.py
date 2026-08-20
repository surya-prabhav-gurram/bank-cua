"""
Observability: a structured, redacted run log plus richer signals on failure.

Every run (discovery or replay) gets its own directory under evidence/ holding:
  * run.jsonl   -- ordered structured events (redacted), one JSON per line
  * summary.json -- final outcome / result contract
  * *.png        -- screenshots (captured each step and on failure)
  * *.dom.html   -- DOM snapshots (redacted), captured on failure

All text passing through here is redacted (name-based + PII patterns), so the
committed evidence never contains secrets or regulated data. Screenshots are the
one exception and are treated as sensitive evidence (see REPORT section 6).
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from ..safety.redaction import redact_mapping, redact_text


class RunLogger:
    def __init__(self, run_dir: str, kind: str, secret_names: Optional[set[str]] = None,
                 secret_values=None):
        self.run_dir = run_dir
        self.kind = kind
        self.secret_names = secret_names or set()
        # `secret_values` may be a list of raw values, or a {param_name: value}
        # mapping. The mapping form makes an over-redaction legible
        # (***REDACTED:username***) when a secret value is also an ordinary
        # word -- the value is still scrubbed either way.
        if isinstance(secret_values, dict):
            self.secret_literals = {k: str(v) for k, v in secret_values.items() if v}
            self.secret_values = list(self.secret_literals.values())
        else:
            self.secret_values = [str(v) for v in (secret_values or []) if v]
            self.secret_literals = list(self.secret_values)
        os.makedirs(run_dir, exist_ok=True)
        self._events: list[dict] = []
        # Deliberately long-lived and flushed per event, not a context manager:
        # a run's log has to survive the run crashing, so events must already be
        # on disk when it does. Closed in finish().
        self._fh = open(os.path.join(run_dir, "run.jsonl"), "w")
        self.event("run_started", kind=kind, run_dir=run_dir)

    # ---- structured events ----------------------------------------------
    def event(self, event_type: str, **fields: Any) -> None:
        rec = {"ts": round(time.time(), 3), "event": event_type}
        rec.update(redact_mapping(fields, self.secret_names))
        # extra pass to catch known secret literals in any stringified field
        line = redact_text(json.dumps(rec, default=str), self.secret_literals)
        self._fh.write(line + "\n")
        self._fh.flush()
        self._events.append(json.loads(line))

    # ---- rich signals ----------------------------------------------------
    def capture(self, surface, label: str, dom: bool = False) -> dict[str, str]:
        paths: dict[str, str] = {}
        shot = os.path.join(self.run_dir, f"{label}.png")
        if surface.screenshot(shot):
            paths["screenshot"] = shot
        if dom:
            html = redact_text(surface.dom_snapshot(), self.secret_literals)
            dom_path = os.path.join(self.run_dir, f"{label}.dom.html")
            with open(dom_path, "w") as f:
                f.write(html)
            paths["dom"] = dom_path
        if paths:
            self.event("evidence_captured", label=label, **paths)
        return paths

    # ---- close -----------------------------------------------------------
    def finish(self, summary: dict[str, Any]) -> str:
        clean = redact_mapping(summary, self.secret_names)
        path = os.path.join(self.run_dir, "summary.json")
        text = redact_text(json.dumps(clean, indent=2, default=str),
                           self.secret_literals)
        with open(path, "w") as f:
            f.write(text)
        self.event("run_finished", summary_path=path)
        try:
            self._fh.close()
        except Exception:
            pass
        return path
