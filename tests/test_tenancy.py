from bankcua.tenancy import TenantOverride, apply_overrides, canonicalize_url
from bankcua.schema import (
    CapabilityArtifact, Target, Step, ActionType, Checkpoint, Locator,
    LocatorCandidate, LocatorKind,
)


def _art():
    return CapabilityArtifact(
        id="c", name="c", description="d",
        target=Target(app_id="a", base_url="http://127.0.0.1:5000",
                      tenant_id="demo-cu", vendor_product="Corebank"),
        steps=[Step(index=0, intent="click search", action=ActionType.CLICK,
                    target=Locator(description="Search button", candidates=[
                        LocatorCandidate(kind=LocatorKind.ROLE, role="button",
                                         value="Search")]),
                    checkpoint=Checkpoint(kind="text_present", value="Member ID"))],
        success=Checkpoint(kind="text_present", value="Member ID"))


def test_apply_overrides_remaps_and_rebinds():
    ov = TenantOverride(tenant_id="summit-cu", base_url="http://127.0.0.1:5002",
                        label_map={"Search": "Find", "Member ID": "Member Number"})
    a = apply_overrides(_art(), ov)
    assert a.target.tenant_id == "summit-cu"
    assert a.target.base_url == "http://127.0.0.1:5002"
    assert a.target.allowed_url_patterns == ["http://127.0.0.1:5002/*",
                                             "http://127.0.0.1:5002"]
    assert a.steps[0].target.candidates[0].value == "Find"
    assert a.steps[0].checkpoint.value == "Member Number"
    assert a.success.value == "Member Number"


def test_overrides_do_not_mutate_original():
    a0 = _art()
    apply_overrides(a0, TenantOverride(tenant_id="x", label_map={"Search": "Find"}))
    assert a0.steps[0].target.candidates[0].value == "Search"  # original intact


def test_canonicalize_url():
    assert canonicalize_url("/member?mid=12345") == "/member?mid=:id"
    assert canonicalize_url("/item/12345/detail") == "/item/:id/detail"
