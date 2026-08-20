"""
Compile a successful discovery transcript into a CapabilityArtifact.

This is where "the model discovers" becomes "a reusable capability." The
compiler:
  * turns each executed transcript step into a typed schema Step,
  * parameterises values/URLs by matching them against the run's input params
    (so a concrete member id 12345 becomes {member_id}),
  * synthesises per-step checkpoints from the observed next state (the
    "did it actually work" guards that make replay deterministic), and turns on
    value verification for fills, whose effect is on the control rather than the
    page,
  * declares typed outputs with the right transform,
  * attaches the vendor's shared KnownConditions (error taxonomy),
  * scrubs run-specific values (both extracted outputs and supplied inputs)
    out of the human-readable step intents, so the artifact describes the
    *flow* and never carries one run's data -- including a sensitive value the
    model happened to echo in its own prose,
  * writes a redacted transcript to disk and references it from provenance
    (the raw transcript is NOT inlined into the artifact).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Optional
from urllib.parse import urlparse

from ..knowledge import conditions_for
from ..replay.errors import apply_transform
from ..safety.redaction import redact_mapping
from ..schema import (
    ActionType,
    ApprovalState,
    CapabilityArtifact,
    Checkpoint,
    Extraction,
    Provenance,
    Step,
    Target,
    ValueSource,
    ValueType,
    WaitSpec,
)
from .loop import DiscoveryResult, TranscriptStep
from .task import DiscoveryTask


def _path_of(url: str) -> str:
    p = urlparse(url)
    return p.path + (("?" + p.query) if p.query else "")


def _parameterise_url(url_or_path: str, param_values: dict, secret: set) -> str:
    out = url_or_path
    for name, val in param_values.items():
        if name in secret or val is None:
            continue
        out = out.replace(str(val), "{" + name + "}")
    return out


def _value_source(raw: Optional[str], param_values: dict,
                  secret: set) -> ValueSource:
    if raw is None:
        return ValueSource(kind="literal", literal="")
    for name, val in param_values.items():
        if val is not None and str(val) == raw:
            kind = "secret_param" if name in secret else "param"
            return ValueSource(kind=kind, param=name)
    return ValueSource(kind="literal", literal=raw)


def _transform_for(output_type: ValueType) -> Optional[str]:
    if output_type == ValueType.MONEY:
        return "money_to_cents"
    if output_type in (ValueType.INTEGER,):
        return "digits_only"
    return "strip"


def compile_artifact(task: DiscoveryTask, result: DiscoveryResult,
                     evidence_dir: str, recorded_by: str,
                     discovery_run_id: str,
                     version: str = "1.0.0") -> CapabilityArtifact:
    secret = {p.name for p in task.inputs if p.sensitive}
    pv = task.param_values
    output_types = {o.name: o.type for o in task.outputs}

    tx = result.transcript
    steps: list[Step] = []
    seen_outputs: set[str] = set()
    for k, rec in enumerate(tx):
        # drop a redundant re-extract of an output already captured (models
        # sometimes extract the same value more than once before finishing)
        if rec.action_kind == "extract":
            if rec.output_name in seen_outputs:
                continue
            seen_outputs.add(rec.output_name)
        # url observed at the NEXT step approximates this step's post-action state
        next_url = tx[k + 1].url if k + 1 < len(tx) else task_base_final(result)
        step = _compile_step(rec, next_url, pv, secret, output_types)
        step.index = len(steps)
        steps.append(step)

    # never bake run-specific extracted VALUES into the reusable artifact: the
    # model may echo a captured value in a step intent -> replace with the
    # output-name placeholder.
    for name, val in result.outputs.items():
        sval = str(val).strip()
        if not sval:
            continue
        # scrub the value as read AND as the caller will receive it: a model
        # often narrates its own transform ("$4,213.55 ... normalized to
        # 421355"), so the derived form has to go too.
        forms = {sval}
        try:
            forms.add(str(apply_transform(
                sval, _transform_for(output_types.get(name, ValueType.STRING)))))
        except Exception:
            pass
        for form in sorted(forms, key=len, reverse=True):
            if len(form) < 3:
                continue
            for step in steps:
                step.intent = step.intent.replace(form, f"<{name}>")

    # ...and the same for the run's INPUT values. The model writes its own prose
    # ("search for member 12345", "the provided operator credentials"), which
    # would otherwise bake one run's data -- and, for a `sensitive` input, an
    # actual credential -- into a reusable, committed artifact. Replacing them
    # with {param} placeholders makes the intent describe the flow and keeps the
    # artifact secret-free. Matched on word boundaries so a short value cannot
    # corrupt unrelated prose; values under 3 chars are left alone as too
    # ambiguous to substitute safely.
    for name, val in pv.items():
        if val is None:
            continue
        sval = str(val)
        if len(sval) < 3:
            continue
        pat = re.compile(rf"(?<!\w){re.escape(sval)}(?!\w)")
        for step in steps:
            step.intent = pat.sub("{" + name + "}", step.intent)

    # write redacted transcript to disk; reference (not inline) from provenance
    os.makedirs(evidence_dir, exist_ok=True)
    tref = os.path.join(evidence_dir, "transcript.json")
    tx_dump = [redact_mapping(r.model_dump(), secret) for r in tx]
    tx_text = json.dumps(tx_dump, indent=2, default=str)
    for p in task.inputs:
        if p.sensitive and pv.get(p.name):
            tx_text = tx_text.replace(str(pv[p.name]), "***REDACTED***")
    with open(tref, "w") as f:
        f.write(tx_text)
    sha = hashlib.sha256(tx_text.encode()).hexdigest()

    base = task.base_url.rstrip("/")
    allow = [f"{base}/*", base]

    return CapabilityArtifact(
        id=task.capability_id, name=task.name, description=task.description,
        version=version,
        target=Target(app_id=task.app_id, surface_type=task.surface_type,
                      base_url=base, entry_path=task.entry_path,
                      tenant_id=task.tenant_id, vendor_product=task.vendor_product,
                      version=task.version, allowed_url_patterns=allow),
        inputs=task.inputs, outputs=task.outputs, steps=steps,
        success=task.success,
        known_conditions=conditions_for(task.vendor_product),
        approval_state=ApprovalState.DRAFT,
        provenance=Provenance(recorded_by=recorded_by,
                              discovery_run_id=discovery_run_id,
                              transcript_ref=tref, transcript_sha256=sha,
                              notes="Compiled from a successful discovery run."),
    )


def task_base_final(result: DiscoveryResult) -> str:
    # best-effort final url for the last step's checkpoint
    return result.transcript[-1].url if result.transcript else ""


def _compile_step(rec: TranscriptStep, next_url: str, pv: dict, secret: set,
                  output_types: dict) -> Step:
    kind = rec.action_kind
    intent = rec.intent or f"{kind} step"
    if kind == "navigate":
        tmpl = _parameterise_url(rec.url_template or "", pv, secret)
        cp = None
        if next_url:
            cp = Checkpoint(kind="url_matches",
                            value=_parameterise_url(_path_of(next_url), pv, secret),
                            description="landed on expected page")
        return Step(index=0, intent=intent, action=ActionType.NAVIGATE,
                    url_template=tmpl, wait=WaitSpec(strategy="load"),
                    checkpoint=cp, risk=rec.risk)
    if kind == "click":
        cp = None
        # if the click changed the page, assert the new url; else assert element gone/next
        if next_url and _path_of(next_url) != _path_of(rec.url):
            cp = Checkpoint(kind="url_matches",
                            value=_parameterise_url(_path_of(next_url), pv, secret),
                            description="navigation after click succeeded")
        return Step(index=0, intent=intent, action=ActionType.CLICK,
                    target=rec.locator, wait=WaitSpec(strategy="load"),
                    checkpoint=cp, risk=rec.risk, risk_reason=rec.risk_reason,
                    requires_confirmation=(rec.risk.value == "risky"))
    if kind == "fill":
        # A fill has no page-state consequence to checkpoint, so it gets the one
        # verification that is meaningful for it: read the control back and
        # confirm the write landed (see Step.verify_value).
        return Step(index=0, intent=intent, action=ActionType.FILL,
                    target=rec.locator, verify_value=True,
                    value=_value_source(rec.value_raw, pv, secret), risk=rec.risk)
    if kind == "select":
        return Step(index=0, intent=intent, action=ActionType.SELECT,
                    target=rec.locator, select_by=rec.select_by,
                    value=_value_source(rec.value_raw, pv, secret), risk=rec.risk)
    if kind == "press":
        cp = None
        if next_url and _path_of(next_url) != _path_of(rec.url):
            cp = Checkpoint(kind="url_matches",
                            value=_parameterise_url(_path_of(next_url), pv, secret))
        return Step(index=0, intent=intent, action=ActionType.PRESS,
                    key=rec.key or "Enter", wait=WaitSpec(strategy="load"),
                    checkpoint=cp)
    if kind == "extract":
        otype = output_types.get(rec.output_name, ValueType.STRING)
        ext = Extraction(output=rec.output_name, locator=rec.locator,
                         attribute=rec.attribute, transform=_transform_for(otype))
        return Step(index=0, intent=intent, action=ActionType.EXTRACT, extract=ext)
    # fallback: assert
    return Step(index=0, intent=intent, action=ActionType.ASSERT)
