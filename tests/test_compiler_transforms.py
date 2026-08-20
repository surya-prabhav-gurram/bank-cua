from bankcua.replay.transforms import apply_transform
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


# ---- binding recorded values back to the parameters they came from ---------
def test_a_decorated_option_label_still_binds_to_its_parameter():
    """Legacy selects render "CODE - Description ($balance)".

    A model choosing that option by label hands the compiler the whole decorated
    string. Matching only exactly leaves it a literal -- and that literal embeds a
    BALANCE, welding the step to one member's account at one moment. A real
    recording carried `'100234-S0070 - Share Draft (Checking) ($232.55)'` and
    could never replay.
    """
    from bankcua.agent.compiler import _value_source
    params = {"member_id": "100234", "from_share": "100234-S0070"}
    v = _value_source("100234-S0070 - Share Draft (Checking) ($232.55)",
                      params, set())
    assert v.kind == "param" and v.param == "from_share"


def test_the_longest_matching_parameter_wins():
    """"100234" is a prefix of the share label too; the share id is the better
    binding and must not be stolen by the member number."""
    from bankcua.agent.compiler import _value_source
    params = {"member_id": "100234", "from_share": "100234-S0070"}
    v = _value_source("100234-S0070 - Regular Shares", params, set())
    assert v.param == "from_share"


def test_a_prefix_must_end_on_a_boundary():
    """"S0001" must not claim an option belonging to "S00013" -- on this target
    those are two different member shares."""
    from bankcua.agent.compiler import _value_source
    v = _value_source("S00013 - Regular Shares", {"share": "S0001"}, set())
    assert v.kind == "literal"


def test_a_secret_is_never_bound_by_prefix():
    """Prefix matching is a convenience for option labels. Applying it to a
    credential could bind an unrelated string to a secret parameter, and a
    secret must only ever match exactly."""
    from bankcua.agent.compiler import _value_source
    v = _value_source("password-ish text", {"password": "password"}, {"password"})
    assert v.kind == "literal"
