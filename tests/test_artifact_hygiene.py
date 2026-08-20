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
    for p in glob.glob(os.path.join(ROOT, "config", "tasks", "**", "*.json"),
                       recursive=True):
        with open(p) as f:
            yield json.load(f)


def _artifact_texts():
    for p in glob.glob(os.path.join(ROOT, "capabilities", "**", "*.json"),
                       recursive=True):
        with open(p) as f:
            yield p, f.read()


def _sensitive_fixtures() -> set[str]:
    out = set()
    for t in _tasks():
        sensitive = {i["name"] for i in t.get("inputs", []) if i.get("sensitive")}
        for name, val in (t.get("param_values") or {}).items():
            if name in sensitive and val:
                out.add(str(val))
    return out


def _prose(task: dict) -> str:
    """The English a capability carries about itself."""
    return " ".join([task.get("description", ""), task.get("goal", ""), task.get("name", "")])


def test_no_sensitive_value_is_persisted_in_any_artifact():
    """Text-scan sweep for credentials that leaked into a committed artifact.

    Secrets whose literal value also occurs in the capability's own prose are
    excluded, and the exclusion is the honest part: Meridian's demo password is
    the word "password", which appears legitimately as a parameter name and in
    step intents like "Enter the operator password". No text scan can separate
    those from a leak, so claiming this sweep covers them would be a claim the
    test does not enforce. Those secrets are covered structurally instead, by
    test_sensitive_inputs_are_referenced_only_by_name below -- which is the
    stronger check anyway, because it asserts the shape rather than hunting for
    a string.
    """
    # A secret is only text-scannable if its literal value is distinctive. Two
    # things make it not: appearing in the capability's own prose, and being the
    # same string as a PARAMETER NAME -- which every artifact contains by
    # construction, since inputs are stored by name. Meridian's demo password is
    # the word "password", which is both.
    prose = " ".join(_prose(t) for t in _tasks())
    identifiers = {i["name"].lower() for t in _tasks() for i in t.get("inputs", [])}
    scannable = {s for s in _sensitive_fixtures()
                 if s.lower() not in prose.lower() and s.lower() not in identifiers}
    assert scannable, "no distinctive sensitive fixtures -- sweep would be vacuous"

    for path, text in _artifact_texts():
        for secret in scannable:
            assert secret not in text, f"secret value {secret!r} persisted in {path}"


def test_sensitive_inputs_are_referenced_only_by_name():
    """The structural invariant, which holds even for a secret that reads like an
    ordinary word.

    A sensitive input must appear in an artifact ONLY as a `secret_param`
    reference. If one ever shows up as a literal value on a step, the artifact is
    carrying a credential regardless of what the text sweep can or cannot see.
    """
    checked = 0
    for path, text in _artifact_texts():
        art = json.loads(text)
        secret_names = {i["name"] for i in art["inputs"] if i.get("sensitive")}
        assert secret_names, f"{os.path.basename(path)} declares no sensitive input"
        fixtures = _sensitive_fixtures()
        for step in art["steps"]:
            value = step.get("value") or {}
            if value.get("param") in secret_names:
                assert value.get("kind") == "secret_param", (
                    f"{os.path.basename(path)} step {step['index']} references a "
                    f"sensitive input as {value.get('kind')!r}, not secret_param")
                assert not value.get("literal"), (
                    f"{os.path.basename(path)} step {step['index']} carries a "
                    f"literal alongside a secret reference")
                checked += 1
            if value.get("kind") == "literal" and value.get("literal"):
                assert value["literal"] not in fixtures, (
                    f"{os.path.basename(path)} step {step['index']} persists a "
                    f"credential as a literal value")
    assert checked, "no secret_param reference found -- test would be vacuous"


def test_step_intents_carry_no_run_specific_input_values():
    """Non-sensitive inputs are scrubbed to {param} placeholders, so one
    recording describes the flow rather than the run it came from.

    Each artifact is checked against ITS OWN task's fixtures. Pooling every
    task's values and testing them against every artifact produces false
    positives across unrelated targets -- round 1's `acct_type: "Money Market"`
    flagged a Meridian artifact whose model happened to use the same English
    words -- and a test that cries wolf is one people learn to silence.

    Known limitation, stated rather than asserted: the compiler can only scrub
    values it was given. A model that describes a parameter's MEANING rather than
    its value ("Select Money Market share type" for `share_type=MMKT`) leaves
    prose that is accurate for the recording and stale for another invocation.
    Intents are documentation for a reviewer, not behaviour, so this is a
    readability cost rather than a correctness one.
    """
    by_capability = {t["capability_id"]: t for t in _tasks()}
    checked = 0
    for path, text in _artifact_texts():
        art = json.loads(text)
        task = by_capability.get(art["id"])
        if task is None:
            continue
        sensitive = {i["name"] for i in task.get("inputs", []) if i.get("sensitive")}
        values = {str(v) for n, v in (task.get("param_values") or {}).items()
                  if n not in sensitive and v and len(str(v)) >= 3}
        intents = " | ".join(s.get("intent", "") for s in art["steps"])
        for v in values:
            assert v not in intents, (
                f"run-specific value {v!r} left in a step intent of {path}")
        checked += 1
    assert checked, "no artifact matched a task -- test would be vacuous"


def _committed_artifact_texts():
    """Artifacts as GIT HAS THEM, not as the working tree has them.

    The invariant is about what ships. Reading the working tree instead means the
    test fails the moment anyone runs the documented demo -- approving a
    capability is the whole point of `scripts/approve_meridian.py` -- and a suite
    that goes red during normal use is one people learn to ignore, which costs
    more than the invariant is worth. Untracked artifacts are skipped: they are
    not committed yet, so the claim does not apply to them.
    """
    import subprocess
    for path, _text in _artifact_texts():
        rel = os.path.relpath(path, ROOT)
        result = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT,
                                capture_output=True, text=True)
        if result.returncode == 0:
            yield path, result.stdout


def test_committed_artifacts_ship_in_their_as_discovered_review_state():
    """A capability is committed as the compiler produced it: draft, with every
    risky step still unratified.

    Approval is a human act performed against a deployment, not a property of the
    recording -- so shipping an artifact pre-approved both misrepresents it and
    quietly disarms the gate for anyone who clones the repo. It also breaks the
    walkthrough that demonstrates the gate: `catalog approve` is documented as
    REFUSED until the risky step is reviewed, and it only refuses while the step
    is genuinely unreviewed.

    Checked against the COMMITTED state so that running the demo, which approves
    capabilities in the working tree, does not turn the suite red.
    """
    checked = 0
    for path, text in _committed_artifact_texts():
        art = json.loads(text)
        risky = [s for s in art["steps"] if s["risk"] == "risky"]
        assert art["approval_state"] == "draft", (
            f"{os.path.basename(path)} is committed as "
            f"{art['approval_state']!r}; approval belongs to a deployment, not "
            f"to the recording (did a demo run mutate it in place?)")
        for s in risky:
            assert not s["risk_reviewed"], (
                f"{os.path.basename(path)} step {s['index']} is COMMITTED "
                f"pre-reviewed, so `catalog approve` will not demonstrate the "
                f"gate for anyone who clones this repo")
            assert "reviewed:" not in s["risk_reason"], (
                f"{os.path.basename(path)} step {s['index']} carries a reviewer "
                f"note from a previous demo run: {s['risk_reason']!r}")
            checked += 1
    assert checked, ("no committed risky step found. Either nothing is committed "
                     "yet, or the approval gate is untested")


def test_provenance_binds_each_artifact_to_a_transcript_that_exists_and_verifies():
    """Provenance is a claim about origin, so it has to be checkable.

    `transcript_ref` + `transcript_sha256` exist to bind an artifact to the
    evidence it was compiled from. A dangling path or a stale hash does not fail
    loudly anywhere -- the artifact still loads, still replays, still looks
    reviewed -- so the mechanism silently stops meaning anything and nobody finds
    out until someone tries to audit a run. Both drifted once already: the
    evidence directories were renamed and the transcripts re-redacted, and
    nothing noticed.
    """
    import hashlib
    checked = 0
    for path, text in _artifact_texts():
        prov = json.loads(text)["provenance"]
        ref, want = prov.get("transcript_ref"), prov.get("transcript_sha256")
        name = os.path.basename(path)
        assert ref, f"{name} records no transcript_ref"
        full = os.path.join(ROOT, ref)
        assert os.path.exists(full), (
            f"{name} points at a transcript that does not exist: {ref} "
            f"(was the evidence directory renamed?)")
        assert want, f"{name} records no transcript_sha256"
        got = hashlib.sha256(open(full).read().encode()).hexdigest()
        assert got == want, (
            f"{name} transcript_sha256 does not match {ref}: the artifact and "
            f"the evidence it claims to come from have drifted apart")
        checked += 1
    assert checked, "no artifact provenance checked -- test would be vacuous"
