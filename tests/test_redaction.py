from bankcua.safety.redaction import redact_text, redact_mapping, REDACTED


def test_pattern_redaction():
    assert "***SSN***" in redact_text("SSN 123-45-6789")
    assert "***CARD***" in redact_text("card 4111111111111111")
    assert "***EMAIL***" in redact_text("mail a.b@x.com")


def test_literal_redaction():
    out = redact_text("password is hunter2", extra_literals=["hunter2"])
    assert "hunter2" not in out and REDACTED in out


def test_mapping_redacts_secret_keys_and_nested():
    data = {"password": "s3cret", "member": "12345",
            "nested": {"password": "again"}, "list": ["a.b@x.com"]}
    out = redact_mapping(data, {"password"})
    assert out["password"] == REDACTED
    assert out["nested"]["password"] == REDACTED
    assert out["member"] == "12345"
    assert "***EMAIL***" in out["list"][0]


def test_card_detection_is_luhn_checked():
    """A 14-digit run id / timestamp is not a card number. Luhn keeps the
    safety-net pattern from corrupting ordinary evidence paths."""
    ts = "evidence/discovery-corebank.member_savings_lookup-20260813-183533"
    assert redact_text(ts) == ts
    assert "***CARD***" not in redact_text("run 20260813183533 finished")
    # a real (Luhn-valid) PAN is still caught, spaced or not
    assert "***CARD***" in redact_text("pan 4111 1111 1111 1111")
    assert "***CARD***" in redact_text("pan 4111111111111111")


def test_literal_redaction_is_token_bounded():
    """A literal may only replace a whole token, so a secret that is also a
    common word cannot corrupt unrelated prose."""
    out = redact_text("cooperative operators cooperate", {"username": "operator"})
    assert out == "cooperative operators cooperate"


def test_named_literal_placeholder_is_legible():
    """Over-redaction is the deliberate bias; naming the parameter keeps the
    evidence readable instead of looking like corrupted output."""
    out = redact_text("the provided operator credentials", {"username": "operator"})
    assert "operator credentials" not in out
    assert "***REDACTED:username***" in out


def test_short_literals_are_not_substituted():
    assert redact_text("row 12 of 12", ["12"]) == "row 12 of 12"
