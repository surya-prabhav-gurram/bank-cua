"""
All three declared RecoveryAction kinds do something.

`click` (dismiss an interstitial) and `reload` (retry a transient 500) are
exercised end-to-end in the evidence scenarios. `wait_retry` -- wait out
transient slowness and re-check, without touching the page -- is the third, and
this is what stops it being an unverified branch. Recovery "success" is defined
throughout as *the condition signature is gone*, never as "the action returned
true", so all three are asserted the same way.
"""
from bankcua.replay.engine import ReplayEngine
from bankcua.schema import (ConditionClass, ConditionDetector, KnownCondition,
                            Locator, LocatorCandidate, LocatorKind, RecoveryAction,
                            Step, ActionType)


class _StubSurface:
    """Minimal surface: the condition clears after `clears_after` detections."""

    def __init__(self, clears_after=0):
        # number of post-recovery checks that still see the condition
        self.clears_after = clears_after
        self.detections = 0
        self.clicked = 0
        self.navigated = 0
        self.waited_ms = 0

    def detect(self, _detector):
        self.detections += 1
        return self.detections <= self.clears_after

    def click(self, _locator):
        self.clicked += 1

    def navigate(self, _url):
        self.navigated += 1

    def current_url(self):
        return "http://127.0.0.1:1/x"

    def wait_ms(self, ms):
        self.waited_ms += ms


class _StubLogger:
    def event(self, *_a, **_kw):
        pass

    def capture(self, *_a, **_kw):
        return {}


def _engine(surface):
    return ReplayEngine(surface, policy=None, logger=_StubLogger())


def _condition(kind, **kw):
    target = Locator(description="dismiss",
                     candidates=[LocatorCandidate(kind=LocatorKind.TEXT,
                                                  value="Continue")])
    return KnownCondition(
        code=f"COND_{kind.upper()}", klass=ConditionClass.RECOVERABLE,
        detector=ConditionDetector(kind="text_present", value="blocked"),
        recovery=RecoveryAction(kind=kind, backoff_ms=1,
                                target=target if kind == "click" else None, **kw))


STEP = Step(index=0, intent="s", action=ActionType.CLICK)


def test_click_recovery_dismisses_and_reports():
    surf = _StubSurface()
    events = []
    assert _engine(surf)._recover(_condition("click"), STEP, events) is True
    assert surf.clicked == 1
    assert surf.clicked == 1 and events[0].attempts == 1
    assert events[0].succeeded and events[0].action == "click"


def test_reload_recovery_navigates_to_the_current_url():
    surf = _StubSurface()
    events = []
    assert _engine(surf)._recover(_condition("reload"), STEP, events) is True
    assert surf.navigated == 1 and surf.clicked == 0


def test_wait_retry_waits_without_touching_the_page():
    """The distinguishing property: transient slowness needs time, not an action.
    Clicking or reloading a half-loaded page is how you turn slow into broken."""
    surf = _StubSurface()
    events = []
    assert _engine(surf)._recover(_condition("wait_retry"), STEP, events) is True
    assert surf.clicked == 0 and surf.navigated == 0
    assert surf.waited_ms > 0
    assert events[0].action == "wait_retry" and events[0].succeeded


def test_recovery_is_bounded_and_failure_is_recorded():
    """A condition that never clears must not loop forever."""
    surf = _StubSurface(clears_after=99)
    events = []
    cond = _condition("wait_retry", max_attempts=3)
    assert _engine(surf)._recover(cond, STEP, events) is False
    assert events[0].attempts == 3 and events[0].succeeded is False


def test_a_condition_with_no_recovery_cannot_recover():
    surf = _StubSurface()
    cond = KnownCondition(code="X", klass=ConditionClass.RECOVERABLE,
                          detector=ConditionDetector(kind="text_present", value="b"))
    assert _engine(surf)._recover(cond, STEP, []) is False
