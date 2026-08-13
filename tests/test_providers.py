"""LLM provider seam: the file-bridge provider and the Anthropic provider's
decision parsing (with a stubbed client, so no network/key needed)."""
import json

import pytest

from bankcua.agent.providers import (BridgeProvider, AnthropicProvider,
                                     DecisionContext, make_provider)


def _ctx(step=0):
    return DecisionContext(goal="g", inputs_hint="", outputs_hint="",
                           observation_text="obs", history="",
                           screenshot_path=None, step_index=step)


def test_bridge_provider_reads_response(tmp_path):
    bridge = str(tmp_path)
    with open(tmp_path / "response-0.json", "w") as f:
        json.dump({"action": "click", "ref": 3, "intent": "go"}, f)
    prov = make_provider("bridge", bridge_dir=bridge, timeout_s=5)
    act = prov.decide(_ctx(0))
    assert act.action == "click" and act.ref == 3
    # it also wrote a request for the operator/model to see
    assert (tmp_path / "request-0.json").exists()


def test_bridge_provider_times_out(tmp_path):
    prov = BridgeProvider(str(tmp_path), timeout_s=0.5, poll_s=0.1)
    with pytest.raises(TimeoutError):
        prov.decide(_ctx(0))


def test_make_provider_unknown():
    with pytest.raises(ValueError):
        make_provider("nope")


class _FakeBlock:
    def __init__(self, type, name=None, input=None, text=None):
        self.type = type
        self.name = name
        self.input = input
        self.text = text


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeClient:
    def __init__(self, blocks):
        self._blocks = blocks
        self.messages = self

    def create(self, **kw):
        return _FakeResp(self._blocks)


def _anthropic(blocks):
    p = AnthropicProvider(model="test-model")
    p.client = _FakeClient(blocks)
    return p


def test_anthropic_provider_parses_tool_call():
    p = _anthropic([_FakeBlock("tool_use", name="act",
                               input={"action": "fill", "ref": 1, "value": "x",
                                      "intent": "type"})])
    act = p.decide(_ctx())
    assert act.action == "fill" and act.ref == 1 and act.value == "x"


def test_anthropic_provider_escalates_without_tool_call():
    p = _anthropic([_FakeBlock("text", text="I am unsure")])
    act = p.decide(_ctx())
    assert act.action == "escalate"


def test_anthropic_provider_requires_model(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(ValueError):
        AnthropicProvider(model=None)
