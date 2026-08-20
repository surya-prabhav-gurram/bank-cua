"""
A checkpoint must not pass because one member number is a prefix of another.

This is the most serious defect found in this project, and it was found by
searching the target by hand rather than by any test. MERIDIAN's member search
matches on SUBSTRING, so `1002` returns several members; the recorded flow then
clicks the first "Select" and lands on `/members/100234`. The step's checkpoint
asked for `/members/{member_id}` -> `/members/1002`, and plain containment says
that matches.

The result was the worst outcome this system can produce. Not an error -- an
ANSWER, reported as `status: success`, returning a different member's balances to
a caller who asked about someone else.

Boundary-aware matching is the fix, and it is the same reasoning already applied
in two other places: redaction matches secrets on word boundaries so a short
value cannot corrupt unrelated text, and the compiler binds an option label to a
parameter only when the prefix ends on a boundary. Substring containment is
almost never what "did we land in the right place" means.
"""
import pytest

from bankcua.replay.matching import url_match

HOST = "https://web-sample.interface-hiring.com"


@pytest.mark.parametrize("expected,actual", [
    # the defect itself, in both directions
    ("/members/1002", f"{HOST}/members/100234"),
    ("/members/100", f"{HOST}/members/100987"),
    ("/members/10023", f"{HOST}/members/100234"),
    # a share id that is a prefix of a longer one on the same member
    ("/shares/100234-S0001", f"{HOST}/shares/100234-S0001-3"),
])
def test_a_prefix_of_a_different_identifier_never_matches(expected, actual):
    assert url_match(expected, actual) is False, (
        f"{expected!r} matched {actual!r} -- a caller asking about one member "
        f"would be handed another member's data and told it succeeded")


@pytest.mark.parametrize("expected,actual", [
    ("/members/100234", f"{HOST}/members/100234"),
    ("/menu", f"{HOST}/menu"),
    ("/members", f"{HOST}/members?by=number&q=100234"),
    ("/members?by=number&q=100234", f"{HOST}/members?by=number&q=100234"),
    ("/members/100234/transfer", f"{HOST}/members/100234/transfer"),
    # a component followed by a further path segment is still that component
    ("/members/100234", f"{HOST}/members/100234/transfer"),
    # and by a fragment
    ("/members/100234", f"{HOST}/members/100234#top"),
])
def test_every_legitimate_checkpoint_still_matches(expected, actual):
    """The fix must not cost precision anywhere it was already correct: these
    are the shapes the compiler actually emits."""
    assert url_match(expected, actual) is True, (
        f"{expected!r} no longer matches {actual!r}; the guard is too strict")


def test_a_later_occurrence_still_counts():
    """The scan continues past a non-boundary hit rather than giving up on the
    first one -- otherwise a value appearing twice, once mid-token and once
    properly, would be rejected."""
    assert url_match("/members/100234",
                     f"{HOST}/members/1002340/redirect?to=/members/100234") is True


def test_an_absent_value_does_not_match():
    assert url_match("/members/999999", f"{HOST}/members/100234") is False


def test_both_surfaces_agree_on_the_same_question():
    """A surface that disagreed with another about "did we land where we meant
    to" would make an artifact's guarantees depend on which driver replayed it."""
    import inspect

    from bankcua.surface.accessibility import AccessibilitySurface
    from bankcua.surface.web_playwright import WebSurface
    for cls in (WebSurface, AccessibilitySurface):
        source = inspect.getsource(cls.check) + inspect.getsource(cls.detect)
        assert "url_match(" in source, (
            f"{cls.__name__} does not use the shared boundary matcher")
        assert "value in self.current_url()" not in source, (
            f"{cls.__name__} still uses bare containment for a URL check")


# ---------------------------------------------------------------------------
# The other half of the same defect: several records matching is a QUESTION
# ---------------------------------------------------------------------------
def test_an_ambiguous_search_is_declared_as_a_business_outcome():
    """Failing hard on an ambiguous search is safe but wrongly typed.

    "Several members matched" is not a fault -- it is a legitimate answer that
    needs more information from the caller, which is precisely what the
    business_outcome class exists for. Reporting it as a failure sends someone to
    investigate a system that is working correctly.
    """
    from bankcua.knowledge import conditions_for
    from bankcua.schema import ActionType, ConditionClass

    c = next(c for c in conditions_for("Meridian")
             if c.code == "AMBIGUOUS_MEMBER_MATCH")
    assert c.klass == ConditionClass.BUSINESS_OUTCOME
    assert c.detector.kind == "element_count_at_least"
    assert c.detector.min_count == 2
    # Only after something was submitted -- the results page does not exist
    # before the search runs.
    assert ActionType.CLICK in c.applies_to_actions
    # The message has to say what would resolve it, or the caller is stuck.
    assert "full member number" in c.message


def test_ambiguity_is_detected_by_count_because_the_page_text_is_identical():
    """A search matching one record and a search matching five render the same
    words. Only the number of rows differs, so no text detector can tell them
    apart -- which is why the detector vocabulary needed a counting kind."""
    from bankcua.knowledge import conditions_for

    codes = [c.code for c in conditions_for("Meridian")]
    counting = [c for c in conditions_for("Meridian")
                if c.detector.kind == "element_count_at_least"]
    assert counting, "no counting detector: ambiguity would be undetectable"
    # Declared before the empty-result condition so ordering never lets the
    # "nothing matched" page speak for a crowded one.
    assert codes.index("AMBIGUOUS_MEMBER_MATCH") < codes.index("NO_SEARCH_MATCHES")


def test_the_counting_detector_is_portable_to_a_surface_with_no_dom():
    """`value` is an accessible NAME, not a selector, so each surface counts in
    its own terms. A CSS selector here would have made every artifact carrying
    this condition unreplayable on the accessibility surface."""
    import inspect

    from bankcua.surface.accessibility import AccessibilitySurface
    from bankcua.surface.web_playwright import WebSurface
    for cls in (WebSurface, AccessibilitySurface):
        assert "element_count_at_least" in inspect.getsource(cls.detect), (
            f"{cls.__name__} cannot evaluate a counting detector")


def test_the_presenter_tells_the_caller_how_to_resolve_the_ambiguity():
    from bankcua.chat.presenter import present
    text = present({"status": "business_outcome", "outputs": {},
                    "business_outcome": {"code": "AMBIGUOUS_MEMBER_MATCH"}})
    assert "full member number" in text
    assert "search by last name" in text
    assert "error" not in text.lower() and "failed" not in text.lower()


def test_every_way_of_failing_to_identify_a_member_asks_rather_than_fails():
    """The principle, stated once: if we cannot confidently identify the member,
    the answer is a QUESTION, not a fault.

    A substring search gives three ways to be uncertain, and all three used to be
    either a wrong answer or a hard failure:

      several matched          -> AMBIGUOUS_MEMBER_MATCH
      one matched, but not the
      number that was asked for -> MEMBER_NUMBER_NOT_EXACT
      nothing matched           -> NO_SEARCH_MATCHES

    None of them is a broken system, and reporting any of them as a failure sends
    an engineer to investigate a search that worked correctly.
    """
    from bankcua.knowledge import conditions_for
    from bankcua.schema import ConditionClass

    codes = {c.code: c for c in conditions_for("Meridian")}
    for code in ("AMBIGUOUS_MEMBER_MATCH", "MEMBER_NUMBER_NOT_EXACT",
                 "NO_SEARCH_MATCHES"):
        assert codes[code].klass == ConditionClass.BUSINESS_OUTCOME, (
            f"{code} is not typed as an answer the caller can act on")
        assert codes[code].message, f"{code} tells the caller nothing"


def test_the_not_exact_condition_is_scoped_to_a_member_record():
    """"The number we asked for is absent" is trivially true on the main menu.

    Without the URL scope this condition fires on the sign-on redirect and every
    capability reports a business outcome about a page nobody was looking at."""
    from bankcua.knowledge import conditions_for
    c = next(c for c in conditions_for("Meridian")
             if c.code == "MEMBER_NUMBER_NOT_EXACT")
    assert c.applies_to_urls == ["/members/"]
    assert c.detector.kind == "text_absent"
    # It compares against the CALLER'S input, which only works because detectors
    # are parameterised like checkpoints.
    assert "{member_id}" in c.detector.value


def test_detectors_are_parameterised_at_evaluation_time():
    """A condition comparing against caller input cannot be written otherwise."""
    import inspect
    from bankcua.replay.engine import ReplayEngine
    source = inspect.getsource(ReplayEngine._handle_conditions)
    assert "_render_detector" in source, (
        "detectors are evaluated raw, so {param} placeholders would never "
        "resolve and the condition would silently never fire")


def test_the_not_exact_condition_requires_two_facts_at_once():
    """Either fact alone is true on pages where the condition does not apply.

    "The member number is absent" holds on the transfer FORM too, which simply
    does not display it -- so on its own the condition fired on the very screen
    the caller had asked for, and four unrelated scenarios started reporting a
    business outcome instead of doing their job. Pinning it to the member RECORD
    as well is what makes it specific.
    """
    from bankcua.knowledge import conditions_for
    c = next(c for c in conditions_for("Meridian")
             if c.code == "MEMBER_NUMBER_NOT_EXACT")
    assert c.also_requires, "condition is broad enough to fire on unrelated pages"
    assert any(d.kind == "text_present" and "MEMBER RECORD" in d.value
               for d in c.also_requires)


def test_a_compound_condition_needs_all_of_its_clauses():
    """Firing on the first clause would make it the broad condition it replaced."""
    import inspect
    from bankcua.replay.engine import ReplayEngine
    source = inspect.getsource(ReplayEngine._handle_conditions)
    assert "also_requires" in source and "all(" in source
