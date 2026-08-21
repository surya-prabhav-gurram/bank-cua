"""
A capability has to actually honour the contract it publishes.

The artifact is a CONTRACT: it names the inputs a caller must supply and the
outputs it promises back. Everything downstream trusts that -- the manifest a
model routes over, the chatbot's argument prompts, the dashboard's invoke form.
Nothing, until now, checked that the recorded STEPS have anything to do with it.

Two invariants, both decidable without launching a browser, and both about the
same failure: a capability that quietly does less than it says.

  * every declared output is produced by some step. An output nobody extracts is
    an OUTPUT_EXTRACTION_FAILED on every single run -- the capability cannot
    succeed, and no amount of replaying it will show why;
  * every required input is consumed by some step. An input nobody types is
    worse, because the run still SUCCEEDS: the caller supplied a new email
    address, the flow posted the form without it, and the outputs read back the
    values that were already there. That is a silent breach -- the one failure
    mode this system treats as worse than an error, because nothing downstream
    knows to check.

Static, so it runs in milliseconds and cannot be flaky. A recording that drops a
step is caught the moment it is committed rather than the next time somebody
demonstrates that capability.
"""
import glob
import json
import os
import re

import pytest

from bankcua.schema import CapabilityArtifact

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")

#: Inputs the SERVICE fills in from the operator's identity, never the caller
#: and never a recorded step (see bankcua/safety/credentials.py). A capability
#: is not expected to "consume" these the way it consumes a member number: they
#: are merged in at invocation time.
SERVICE_SUPPLIED = {"operator", "password", "branch"}

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _artifacts():
    paths = sorted(glob.glob(os.path.join(ROOT, "capabilities", "**", "*.json"),
                             recursive=True))
    if not paths:
        pytest.skip("no capabilities recorded")
    for path in paths:
        with open(path) as fh:
            yield os.path.relpath(path, ROOT), CapabilityArtifact.from_json(fh.read())


def _consumed_names(art: CapabilityArtifact) -> set[str]:
    """Every parameter name the recorded steps actually reference.

    Two ways a step can name a parameter, and both count: a `ValueSource` of kind
    param/secret_param, and a `{placeholder}` inside any string the engine
    renders (url templates, checkpoints, detectors). Scanning the serialised form
    rather than a hand-written list of fields means a schema addition cannot
    quietly create a third way that this misses.
    """
    names: set[str] = set()
    raw = json.loads(art.model_dump_json())

    def walk(node):
        if isinstance(node, dict):
            if node.get("kind") in ("param", "secret_param") and node.get("param"):
                names.add(node["param"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            names.update(_PLACEHOLDER.findall(node))

    walk(raw)
    return names


@pytest.mark.parametrize("rel,art", list(_artifacts()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_every_declared_output_is_produced_by_a_step(rel, art):
    """A promised output with no extract step behind it can never be returned.

    The replay engine enforces the contract at the end of a run
    (OUTPUT_EXTRACTION_FAILED), which is the right place to enforce it but a
    terrible place to LEARN it: the capability has already signed on, walked a
    member's record and posted whatever it posts, and only then reports that it
    was never able to answer.
    """
    produced = {st.extract.output for st in art.steps if st.extract}
    missing = [o.name for o in art.outputs if o.name not in produced]
    assert not missing, (
        f"{rel} declares output(s) {missing} that no step extracts, so every "
        f"run of it ends OUTPUT_EXTRACTION_FAILED. Steps that do extract: "
        f"{sorted(produced)}")


@pytest.mark.parametrize("rel,art", list(_artifacts()),
                         ids=lambda v: v if isinstance(v, str) else "")
def test_every_required_input_is_consumed_by_a_step(rel, art):
    """An input the caller must supply, that no step ever uses, is a lie.

    This is the more dangerous of the two invariants. A missing OUTPUT fails
    loudly. A missing INPUT does not fail at all: the flow posts a form nobody
    typed into, the host accepts it, and the values read back afterwards are the
    ones that were already on the record -- so a caller who asked to change an
    email address is told it worked, with the old address as evidence.
    """
    consumed = _consumed_names(art)
    unused = [p.name for p in art.inputs
              if p.required and p.name not in SERVICE_SUPPLIED
              and p.name not in consumed]
    assert not unused, (
        f"{rel} requires input(s) {unused} that no step reads. Either a step "
        f"that used them was lost when the capability was recorded, or the "
        f"contract asks for something it does not need -- and until one of "
        f"those is true, a caller supplying them is told they were applied.")
