from bankcua.replay.errors import apply_transform
from bankcua.agent.compiler import _parameterise_url, _value_source


def test_money_transform():
    assert apply_transform("$4,213.55", "money_to_cents") == 421355
    assert apply_transform("$0.00", "money_to_cents") == 0


def test_digits_transform():
    assert apply_transform("SA-12345", "digits_only") == 12345


def test_strip_transform():
    assert apply_transform("  hi  ", "strip") == "hi"


def test_url_parameterisation_skips_secrets():
    pv = {"member_id": "12345", "password": "p"}
    out = _parameterise_url("/member?mid=12345", pv, secret={"password"})
    assert out == "/member?mid={member_id}"
    # secret values are NOT substituted into the (persisted) URL template
    out2 = _parameterise_url("/x?token=p", pv, secret={"password"})
    assert out2 == "/x?token=p"


def test_value_source_matching():
    pv = {"member_id": "12345", "password": "p"}
    vs = _value_source("12345", pv, secret={"password"})
    assert vs.kind == "param" and vs.param == "member_id"
    vs2 = _value_source("p", pv, secret={"password"})
    assert vs2.kind == "secret_param" and vs2.param == "password"
    vs3 = _value_source("literalthing", pv, secret={"password"})
    assert vs3.kind == "literal" and vs3.literal == "literalthing"


def _mini_artifact(param_values, outputs, intents, inputs, out_fields):
    """Compile a tiny navigate-only transcript so intent scrubbing can be
    asserted without a browser."""
    import tempfile
    from bankcua.agent.compiler import compile_artifact
    from bankcua.agent.loop import DiscoveryResult, TranscriptStep
    from bankcua.agent.task import DiscoveryTask
    from bankcua.schema import Checkpoint

    task = DiscoveryTask(
        capability_id="t.cap", name="t", description="t", goal="g",
        app_id="a", base_url="http://127.0.0.1:1", inputs=inputs,
        outputs=out_fields, success=Checkpoint(kind="url_matches", value="/x"),
        param_values=param_values)
    tx = [TranscriptStep(index=i, intent=t, action_kind="navigate",
                         url="http://127.0.0.1:1/x", url_template="/x")
          for i, t in enumerate(intents)]
    res = DiscoveryResult(status="success", transcript=tx, outputs=outputs)
    with tempfile.TemporaryDirectory() as d:
        art = compile_artifact(task, res, d, "test", "run-1")
    return art


def test_intents_are_parameterised_and_secret_free():
    """The model narrates in prose ("the provided operator credentials"), which
    would otherwise bake one run's data -- and a real credential -- into a
    reusable, committed artifact."""
    from bankcua.schema import InputParameter, OutputField, ValueType

    art = _mini_artifact(
        param_values={"username": "operator", "member_id": "12345"},
        outputs={"balance": "$4,213.55"},
        intents=["Fill in the username with the provided operator credentials",
                 "Search for member 12345",
                 "Read the balance: $4,213.55 i.e. 421355 cents"],
        inputs=[InputParameter(name="username", sensitive=True),
                InputParameter(name="member_id")],
        out_fields=[OutputField(name="balance", type=ValueType.MONEY)])

    text = " | ".join(s.intent for s in art.steps)
    assert "operator" not in text            # sensitive value scrubbed
    assert "12345" not in text               # run-specific input scrubbed
    assert "$4,213.55" not in text           # extracted value scrubbed
    assert "421355" not in text              # ...and its transformed form
    assert "{username}" in text and "{member_id}" in text
    assert "<balance>" in text
