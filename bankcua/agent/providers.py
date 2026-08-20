"""
LLM provider seam for the discovery loop.

The loop is provider-agnostic: it hands a `DecisionContext` to a provider and
gets back a validated `DiscoveryAction`. Two implementations:

  * AnthropicProvider -- the PRODUCTION / unattended path. Calls a hosted LLM
    Messages API with tool-use, sends the observation text plus a screenshot,
    and forces a single `act` tool call. Requires an API key + model id.

  * BridgeProvider -- externalises each decision to an interactive decision
    source (a human operator, or an LLM run out-of-band). It writes each
    decision request (rendered observation + screenshot path) to a bridge
    directory and blocks until a response JSON appears. This is the seam used to
    drive a discovery run when the API path is not wired up, and to reproduce a
    recorded run deterministically from a saved decision trace.

Both return the identical structured action, so the recorded run is the same
regardless of which source produced the decisions.
"""
from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from .actions import ACT_TOOL, DiscoveryAction
from .prompts import SYSTEM_PROMPT, build_user_message


@dataclass
class DecisionContext:
    goal: str
    inputs_hint: str
    outputs_hint: str
    observation_text: str
    history: str
    screenshot_path: Optional[str]
    step_index: int
    #: The structured observation behind `observation_text`. Model-backed
    #: providers ignore it -- an LLM is given the rendering, not our objects --
    #: but a provider that resolves targets programmatically needs the refs, and
    #: re-parsing them back out of the rendered text would be a parser nobody
    #: should have to maintain.
    observation: Optional[object] = None


class LLMProvider:
    name = "base"

    def decide(self, ctx: DecisionContext) -> DiscoveryAction:  # pragma: no cover
        raise NotImplementedError


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, model: str | None = None, max_tokens: int = 1024):
        import anthropic
        self.client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
        self.model = model or os.environ.get("LLM_MODEL")
        if not self.model:
            raise ValueError(
                "no model id supplied; pass --model or set the LLM_MODEL env var")
        self.max_tokens = max_tokens

    def decide(self, ctx: DecisionContext) -> DiscoveryAction:
        content: list[dict] = []
        if ctx.screenshot_path and os.path.exists(ctx.screenshot_path):
            with open(ctx.screenshot_path, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode()
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": b64}})
        content.append({"type": "text", "text": build_user_message(
            ctx.goal, ctx.inputs_hint, ctx.outputs_hint,
            ctx.observation_text, ctx.history)})

        resp = self.client.messages.create(
            model=self.model, max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT, tools=[ACT_TOOL],
            tool_choice={"type": "tool", "name": "act"},
            messages=[{"role": "user", "content": content}],
        )
        for block in resp.content:
            if block.type == "tool_use" and block.name == "act":
                return DiscoveryAction.model_validate(block.input)
        # model didn't call the tool -> escalate rather than guess
        return DiscoveryAction(action="escalate",
                               intent="no tool call returned",
                               reason="model did not produce a structured action")


class BridgeProvider(LLMProvider):
    """Externalises each decision to an interactive source (see module doc)."""
    name = "bridge"

    def __init__(self, bridge_dir: str, timeout_s: float = 1800.0, poll_s: float = 2.0):
        self.dir = bridge_dir
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        os.makedirs(bridge_dir, exist_ok=True)

    def decide(self, ctx: DecisionContext) -> DiscoveryAction:
        n = ctx.step_index
        req_path = os.path.join(self.dir, f"request-{n}.json")
        resp_path = os.path.join(self.dir, f"response-{n}.json")
        req = {
            "step_index": n,
            "goal": ctx.goal,
            "inputs_hint": ctx.inputs_hint,
            "outputs_hint": ctx.outputs_hint,
            "history": ctx.history,
            "observation": ctx.observation_text,
            "screenshot_path": ctx.screenshot_path,
            "instructions": ("Read the screenshot + observation, then WRITE this "
                             f"file: {resp_path} containing a DiscoveryAction JSON "
                             "(fields: action, intent, ref?, url?, value?, "
                             "select_by?, output_name?, attribute?, success?, reason?)."),
        }
        with open(req_path, "w") as f:
            json.dump(req, f, indent=2)

        deadline = time.time() + self.timeout_s
        while time.time() < deadline:
            if os.path.exists(resp_path):
                with open(resp_path) as f:
                    data = json.load(f)
                return DiscoveryAction.model_validate(data)
            time.sleep(self.poll_s)
        raise TimeoutError(f"no bridge decision for step {n} within {self.timeout_s}s")


def make_provider(kind: str, **kw) -> LLMProvider:
    if kind == "scripted":
        return ScriptedProvider(kw["steps"])
    if kind == "anthropic":
        return AnthropicProvider(**{k: v for k, v in kw.items()
                                    if k in ("model", "max_tokens")})
    if kind == "bridge":
        return BridgeProvider(kw["bridge_dir"],
                              timeout_s=kw.get("timeout_s", 1800.0))
    raise ValueError(f"unknown provider {kind}")


class ScriptedProvider(LLMProvider):
    """Replay a recorded decision trace, addressing controls semantically.

    What this is, and what it is not
    --------------------------------
    This is the MOCKED LLM BOUNDARY for the Meridian capabilities. Round 1's
    committed evidence is a genuine live Anthropic run (see
    `evidence/discovery-*-anthropic-live/`); these recordings were produced
    without an API key, so the decisions are supplied from a trace instead of
    generated. Everything downstream of the decision is real: the same discovery
    loop, the same live browser, the same locator synthesis, the same safety
    pre-flight, the same compiler. What is mocked is only who chose the action.

    Why targets are semantic rather than integer refs
    -------------------------------------------------
    `BridgeProvider` addresses elements by the integer `ref` from the
    observation, which is what a model does. A hand-authored trace of integers is
    a different thing: it is unreadable, it silently retargets when a page gains
    a control, and a reviewer cannot tell whether `ref 7` was the intended
    button. Naming the target ("the control beside 'Operator ID:'", "the button
    called 'Post Transfer'") keeps the trace reviewable and makes a mis-record
    fail loudly instead of clicking the wrong thing.

    The seam is unchanged: this returns the same validated `DiscoveryAction` as
    the Anthropic provider, so the loop cannot tell them apart.
    """
    name = "scripted"

    def __init__(self, steps: list[dict]):
        self.steps = steps

    def decide(self, ctx: DecisionContext) -> DiscoveryAction:
        if ctx.step_index >= len(self.steps):
            return DiscoveryAction(action="finish", intent="trace exhausted",
                                   success=True, reason="end of recorded trace")
        spec = dict(self.steps[ctx.step_index])
        target = spec.pop("target", None)
        if target is not None:
            spec["ref"] = self._resolve_ref(target, ctx)
        return DiscoveryAction.model_validate(spec)

    @staticmethod
    def _resolve_ref(target: dict, ctx: DecisionContext) -> int:
        obs = ctx.observation
        if obs is None:
            raise RuntimeError("scripted provider needs a structured observation")
        want_name = target.get("name")
        want_near = target.get("near_label")
        want_readout = target.get("readout")

        if want_readout is not None:
            for r in obs.readouts:
                if r.label.strip().rstrip(":") == want_readout.rstrip(":"):
                    return r.ref
            raise RuntimeError(
                f"step {ctx.step_index}: no readout labelled {want_readout!r}; "
                f"saw {[r.label for r in obs.readouts][:12]}")

        for e in obs.elements:
            if want_name is not None and (e.name or "").strip() == want_name:
                return e.ref
            if want_near is not None and (e.near_label or "").strip() == want_near:
                return e.ref
        raise RuntimeError(
            f"step {ctx.step_index}: no element matching {target}; saw "
            f"{[(e.ref, e.name or e.near_label) for e in obs.elements][:12]}")
