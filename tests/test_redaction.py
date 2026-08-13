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
