"""
Repo invariant: committed artifacts carry a flow, never a run.

REPORT section 6 claims a sweep of capabilities/ finds no secret. This test is
that claim, enforced -- so it cannot silently regress the next time a capability
is recorded. Sensitive values must never be persisted, and non-sensitive run
values are parameterised so the artifact stays reusable.
"""
import glob
import json
import os

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")


def _tasks():
    for p in glob.glob(os.path.join(ROOT, "config", "tasks", "*.json")):
        with open(p) as f:
            yield json.load(f)


def _artifact_texts():
    for p in glob.glob(os.path.join(ROOT, "capabilities", "*.json")):
        with open(p) as f:
            yield p, f.read()


def test_no_sensitive_value_is_persisted_in_any_artifact():
    secrets = set()
    for t in _tasks():
        sensitive = {i["name"] for i in t.get("inputs", []) if i.get("sensitive")}
        for name, val in (t.get("param_values") or {}).items():
            if name in sensitive and val:
                secrets.add(str(val))
    assert secrets, "no sensitive fixtures found -- test would be vacuous"

    for path, text in _artifact_texts():
        for s in secrets:
            assert s not in text, f"secret value {s!r} persisted in {path}"


def test_step_intents_carry_no_run_specific_input_values():
    """Non-sensitive inputs are scrubbed to {param} placeholders, so one
    recording describes the flow rather than the run it came from."""
    values = set()
    for t in _tasks():
        sensitive = {i["name"] for i in t.get("inputs", []) if i.get("sensitive")}
        for name, val in (t.get("param_values") or {}).items():
            if name not in sensitive and val and len(str(val)) >= 3:
                values.add(str(val))

    for path, text in _artifact_texts():
        art = json.loads(text)
        intents = " | ".join(s.get("intent", "") for s in art["steps"])
        for v in values:
            assert v not in intents, (
                f"run-specific value {v!r} left in a step intent of {path}")
