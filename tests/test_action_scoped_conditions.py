"""
One page signature, two meanings, decided by what the step just did.

MERIDIAN renders "TRANSACTION REJECTED" both when a submitted form genuinely
fails validation and when its fault injector fires on an ordinary page load.
Those need opposite handling, and a taxonomy keyed only on page text cannot tell
them apart -- so it must pick one answer and be wrong half the time:

  * treat it as a business outcome always -> a random blip during a navigation is
    reported to the caller as "the bank rejected your transaction", about a
    transaction that was never submitted;
  * treat it as recoverable always -> a real rejection is retried pointlessly and
    then surfaced as a recovery failure, which reads as a system fault.

Driven through a stub surface rather than a browser: the behaviour under test is
the engine's classification, and a real page would make the test slower without
making it stricter.
"""
from bankcua.observability.logging import RunLogger
from bankcua.replay.engine import ReplayEngine
from bankcua.replay.result import ReplayStatus
from bankcua.safety.policy import Policy, PolicyEngine
from bankcua.schema import (ActionType, CapabilityArtifact, Checkpoint,
                            InputParameter, Locator, LocatorCandidate,
                            LocatorKind, Step, Target)
from bankcua.surface.base import ActResult, Surface

REJECTED = "TRANSACTION REJECTED"


class _StubSurface(Surface):
    """Shows the rejection page until a given number of navigations have happened.

    Counted in TOTAL navigations, which includes the two the engine performs
    before any recovery can run: one to the artifact's entry path, one for the
    step itself. So `clears_after=3` means "heals on the first recovery reload".
    Counting only recovery reloads would hide that ordering, and the ordering is
    the thing these tests are about.

    `clears_after=None` never heals: a real rejection, not a transient fault.
    """
    supported_locator_kinds = frozenset(LocatorKind)

    #: Navigations the engine makes before recovery is possible: entry + step.
    NAVS_BEFORE_RECOVERY = 2

    def __init__(self, clears_after: int | None):
        self.clears_after = clears_after
        self.navigations = 0

    @property
    def recovery_reloads(self) -> int:
        return max(0, self.navigations - self.NAVS_BEFORE_RECOVERY)

    def _rejecting(self) -> bool:
        if self.clears_after is None:
            return True
        return self.navigations < self.clears_after

    def start(self): ...
    def stop(self): ...
    def observe(self): raise NotImplementedError
    def index_elements(self): return []
    def index_readouts(self, start_ref): return []
    def locator_for_element(self, element): raise NotImplementedError
    def navigate(self, url):
        self.navigations += 1
        return ActResult(ok=True, message="navigated")
    def click(self, locator): return ActResult(ok=True, message="clicked")
    def fill(self, locator, text): return ActResult(ok=True, message="filled")
    def select_option(self, locator, value, by="value"): return ActResult(ok=True)
    def press(self, key): return ActResult(ok=True)
    def read(self, locator, attribute="text"): return ActResult(ok=True, value="x")
    def check(self, checkpoint): return not self._rejecting()
    def detect(self, detector):
        return detector.value == REJECTED and self._rejecting()
    def screenshot(self, path): return None
    def dom_snapshot(self): return ""
    def current_url(self): return "https://target/x"
    def wait_ms(self, ms): ...


LOC = Locator(description="probe", candidates=[
    LocatorCandidate(kind=LocatorKind.TEXT, value="x")])


def _artifact(action: ActionType) -> CapabilityArtifact:
    from bankcua.knowledge import conditions_for
    step = Step(index=0, intent="probe", action=action, target=LOC,
                url_template="/x" if action == ActionType.NAVIGATE else None)
    return CapabilityArtifact(
        id="probe.cap", name="probe", description="probe",
        target=Target(app_id="meridian-core", base_url="https://target",
                      vendor_product="Meridian",
                      allowed_url_patterns=["https://target/*"]),
        inputs=[InputParameter(name="member_id", required=False)],
        outputs=[], steps=[step],
        success=Checkpoint(kind="text_present", value="OK"),
        known_conditions=conditions_for("Meridian"))


def _run(action, clears_after, tmp_path, name):
    surface = _StubSurface(clears_after)
    policy = PolicyEngine(Policy(allowed_url_patterns=["https://target/*"]),
                          allow_risky_override=True)
    logger = RunLogger(str(tmp_path / name), "replay", set(), [])
    res = ReplayEngine(surface, policy, logger, None).run(_artifact(action), {})
    return res, surface


def test_a_submitted_rejection_is_a_business_outcome(tmp_path):
    """The caller submitted something and the application said no. That is an
    answer, and retrying it would be both useless and misleading."""
    res, surface = _run(ActionType.CLICK, None, tmp_path, "submit")
    assert res.status == ReplayStatus.BUSINESS_OUTCOME
    assert res.business_outcome.code == "TRANSACTION_REJECTED"
    assert surface.recovery_reloads == 0, "a real rejection must never be retried"
    assert res.recoveries == []


def test_the_same_page_on_a_navigation_is_retried_instead(tmp_path):
    """Nothing was submitted, so nothing was rejected. The unscoped
    INJECTED_FAULT entry catches it and the run continues."""
    res, surface = _run(ActionType.NAVIGATE, 3, tmp_path, "navfault")
    assert res.status == ReplayStatus.SUCCESS
    assert surface.recovery_reloads >= 1, "the fault was not retried"
    assert [r.condition_code for r in res.recoveries] == ["INJECTED_FAULT"]
    assert res.recoveries[0].succeeded is True


def test_a_navigation_fault_that_never_clears_still_fails_cleanly(tmp_path):
    """Bounded, not infinite: when the retry budget is spent the run stops and
    says which condition defeated it, rather than looping against the target."""
    res, surface = _run(ActionType.NAVIGATE, None, tmp_path, "stuck")
    assert res.status == ReplayStatus.FAILURE
    assert res.failure.code == "RECOVERY_FAILED_INJECTED_FAULT"
    # max_attempts: 3 in config/knowledge/meridian.yaml
    assert surface.recovery_reloads == 3, "retry budget was not honoured"
    assert res.recoveries[-1].succeeded is False


def test_scoping_does_not_let_an_inapplicable_condition_shadow_a_match(tmp_path):
    """The scope check runs BEFORE detection, so a condition that does not apply
    cannot consume the match and hide the one that does. If it did, the
    navigation case above would silently classify as a business outcome."""
    from bankcua.knowledge import conditions_for
    conds = conditions_for("Meridian")
    rejected = [c for c in conds if c.detector.value == REJECTED]
    assert len(rejected) == 2, "both readings of this page must be declared"
    scoped, unscoped = rejected[0], rejected[1]
    assert scoped.applies_to_actions and not unscoped.applies_to_actions
    assert conds.index(scoped) < conds.index(unscoped), (
        "the scoped business outcome must be declared first, or the recoverable "
        "one wins on submitting steps too")
