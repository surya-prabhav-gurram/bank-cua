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
