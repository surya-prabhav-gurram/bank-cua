from bankcua.schema import (
    CapabilityArtifact, Checkpoint, InputParameter, OutputField, Step,
    ActionType, Target, ValueSource, ValueType, Locator, LocatorCandidate,
    LocatorKind,
)


def _artifact():
    return CapabilityArtifact(
        id="x.cap", name="Cap", description="desc",
        target=Target(app_id="app", base_url="http://h", entry_path="/login"),
        inputs=[InputParameter(name="member_id", type=ValueType.STRING),
                InputParameter(name="pw", sensitive=True)],
        outputs=[OutputField(name="bal", type=ValueType.MONEY)],
        steps=[Step(index=0, intent="go", action=ActionType.NAVIGATE,
                    url_template="/member?mid={member_id}")],
        success=Checkpoint(kind="text_present", value="ok"),
    )


def test_roundtrip_json():
    a = _artifact()
    b = CapabilityArtifact.from_json(a.to_json())
    assert b.id == a.id
    assert b.steps[0].url_template == "/member?mid={member_id}"
    assert b.secret_params() == {"pw"}
    assert b.output_names() == ["bal"]


def test_validate_inputs():
    a = _artifact()
    a.validate_inputs({"member_id": "1", "pw": "x"})
    try:
        a.validate_inputs({"pw": "x"})
        assert False, "should have raised"
    except ValueError as e:
        assert "member_id" in str(e)


def test_value_source_resolve():
    assert ValueSource(kind="literal", literal="hi").resolve({}, {}) == "hi"
    assert ValueSource(kind="param", param="m").resolve({"m": 5}, {}) == "5"
    assert ValueSource(kind="secret_param", param="pw").resolve({"pw": "s"}, {}) == "s"


def test_locator_requires_candidate():
    loc = Locator(description="d",
                  candidates=[LocatorCandidate(kind=LocatorKind.ROLE, role="button",
                                               value="Go")])
    assert loc.candidates[0].role == "button"
