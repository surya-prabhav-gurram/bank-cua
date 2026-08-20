"""
Direct tests of the Playwright web surface: element/readout indexing, locator
synthesis + resolution across candidates and frames, every checkpoint and
detector kind, coordinate clicks, attribute reads, and injected 500 handling.
"""
import urllib.request

import pytest

pytest.importorskip("playwright")

from bankcua.surface.web_playwright import WebSurface
from bankcua.schema import (Checkpoint, ConditionDetector, Locator,
                            LocatorCandidate, LocatorKind)


@pytest.fixture
def surf(mock_app):
    s = WebSurface(mock_app, headless=True)
    s.start()
    yield s
    s.stop()


def _login(s):
    s.navigate("/login")
    els = s.index_elements()
    s.fill(s.locator_for_element(els[0]), "operator")
    s.fill(s.locator_for_element(els[1]), "password123")
    s.click(s.locator_for_element(els[2]))


def test_index_elements_and_locator_ordering(surf):
    surf.navigate("/login")
    els = surf.index_elements()
    assert [e.role for e in els] == ["textbox", "textbox", "button"]
    # button gets a semantic role+name candidate first
    loc = surf.locator_for_element(els[2])
    assert loc.candidates[0].kind == LocatorKind.ROLE
    assert loc.candidates[0].value == "Sign On"
    # An unlabelled input is addressable only by proximity. The PORTABLE form of
    # that intent comes first (a non-DOM surface can resolve it spatially), the
    # DOM-bound form second, and structural paths last.
    uloc = surf.locator_for_element(els[0])
    assert uloc.candidates[0].kind == LocatorKind.NEAR_LABEL
    assert uloc.candidates[0].value == "User ID"
    assert uloc.candidates[1].kind == LocatorKind.XPATH
    assert 'User ID' in uloc.candidates[0].value


def test_readout_locators_lead_with_the_portable_candidate(surf):
    """An extraction target must be addressable on a surface with no DOM.

    A read-only value carries no accessible name of its own -- "$4,213.55" is not
    called anything -- so proximity to its label is both the durable handle and
    the only strategy a non-DOM surface can honour. Recording the XPath first
    would make every extract step unportable, which would be a property of how we
    recorded it rather than of the flow. That failure is invisible at record time
    and only shows up as an unreachable step mid-run, so it is pinned here.
    """
    _login(surf)
    surf.navigate("/member?mid=12345")
    savings = next(r for r in surf.observe().readouts if r.label == "Savings")
    kinds = [c.kind for c in savings.locator.candidates]
    assert kinds[0] == LocatorKind.NEAR_LABEL
    assert savings.locator.candidates[0].value == "Savings"
    assert LocatorKind.XPATH in kinds          # DOM-bound form still recorded
    # and it must actually resolve on THIS surface, not merely be recorded
    assert surf.read(savings.locator, "text").value == "$4,213.55"


def test_readouts_and_frame_read(surf):
    _login(surf)
    surf.navigate("/member?mid=12345")
    obs = surf.observe()
    labels = {r.label for r in obs.readouts}
    assert "Name" in labels and "Savings" in labels
    # read the balance from INSIDE the iframe
    bal = Locator(description="bal", frame_path=["balancepane"],
                  candidates=[LocatorCandidate(kind=LocatorKind.TEXT, value="$4,213.55")])
    assert surf.read(bal, "text").value == "$4,213.55"


def test_read_attributes_text_value_href(surf):
    _login(surf)
    surf.navigate("/member?mid=12345")
    # href of the sub-account link (role=link targets the <a>, not inner text)
    link = Locator(description="link", candidates=[LocatorCandidate(
        kind=LocatorKind.ROLE, role="link", value="Open New Sub-Account")])
    assert "/subaccount/new" in surf.read(link, "href").value
    # value attr on a plain cell falls back to text
    name = Locator(description="name", candidates=[LocatorCandidate(
        kind=LocatorKind.XPATH, value='//tr[td[normalize-space(.)="Name"]]/td[last()]')])
    assert surf.read(name, "value").value == "Jane A. Doe"


def test_select_and_press(surf):
    _login(surf)
    surf.navigate("/subaccount/new?mid=12345")
    els = surf.index_elements()
    combo = next(e for e in els if e.role == "combobox")
    assert surf.select_option(surf.locator_for_element(combo), "Money Market",
                              by="label").ok
    assert surf.press("Tab").ok


def test_all_checkpoint_kinds(surf):
    _login(surf)
    surf.navigate("/member?mid=12345")
    assert surf.check(Checkpoint(kind="url_matches", value="/member?mid=12345"))
    assert surf.check(Checkpoint(kind="text_present", value="Jane A. Doe"))
    assert surf.check(Checkpoint(kind="text_absent", value="No member found"))
    assert surf.check(Checkpoint(kind="http_status_lt", value="400"))
    assert surf.check(Checkpoint(kind="text_present", value="Savings",
                                 frame_path=["balancepane"]))
    assert not surf.check(Checkpoint(kind="text_present", value="does not exist"))


def test_all_detector_kinds(surf):
    _login(surf)
    surf.navigate("/member?mid=00000")     # not found page
    assert surf.detect(ConditionDetector(kind="text_present", value="No member found"))
    assert surf.detect(ConditionDetector(kind="url_matches", value="mid=00000"))
    assert not surf.detect(ConditionDetector(kind="text_present", value="Jane A. Doe"))


def test_injected_500_sets_status(surf, mock_app):
    urllib.request.urlopen(f"{mock_app}/_control/set?key=inject&value=error500").read()
    try:
        surf.navigate("/home")
        assert surf.detect(ConditionDetector(kind="http_status", value="500"))
        assert surf.detect(ConditionDetector(kind="text_present", value="Application Error"))
    finally:
        urllib.request.urlopen(f"{mock_app}/_control/reset").read()


def test_coordinate_click_does_not_crash(surf):
    surf.navigate("/login")
    loc = Locator(description="mid-screen", candidates=[LocatorCandidate(
        kind=LocatorKind.COORDINATES, value="0.5,0.5")])
    assert surf.click(loc).ok


def test_unresolvable_locator_reports_failure(surf):
    surf.navigate("/login")
    loc = Locator(description="ghost", candidates=[LocatorCandidate(
        kind=LocatorKind.CSS, value="button#does-not-exist")])
    res = surf.click(loc)
    assert not res.ok and "resolve" in res.message


SELECT_PAGE = """
<html><body><table>
<tr><td>From Share:</td><td><select name="from">
  <option value="100234-S0001">100234-S0001 - Regular Shares ($1,499.00)</option>
  <option value="100234-S0001-3">100234-S0001-3 - Regular Shares ($10.00)</option>
  <option value="100234-S0070">100234-S0070 - Share Draft (Checking) ($226.55)</option>
</select></td></tr></table></body></html>
"""

SELECT_LOC = Locator(description="from share", candidates=[
    LocatorCandidate(kind=LocatorKind.NEAR_LABEL, value="From Share:")])


@pytest.fixture
def select_surface(surf):
    surf._page.set_content(SELECT_PAGE)
    return surf


def _selected(s):
    return s._page.eval_on_selector("select", "el => el.value")


def test_an_option_resolves_by_value_even_when_the_recording_said_label(select_surface):
    """Whether the recorded string is the option's value or its visible label is
    a property of the markup, not of the intent -- and a live model records
    `select_by=label` by default while naming a code."""
    res = select_surface.select_option(SELECT_LOC, "100234-S0070", by="label")
    assert res.ok and _selected(select_surface) == "100234-S0070"
    assert "matched on value" in res.message, (
        "a fallback that resolved must say so, or a recording that only works "
        "via the fallback looks indistinguishable from one that is correct")


def test_a_leading_code_matches_the_full_label(select_surface):
    """Legacy selects render "CODE - Long Description" over a bare code."""
    select_surface._page.set_content(SELECT_PAGE.replace(
        'value="100234-S0070"', 'value="ignored-0070"'))
    res = select_surface.select_option(SELECT_LOC, "100234-S0070", by="value")
    assert res.ok and "leading code" in res.message


def test_an_exact_match_always_beats_a_prefix_match(select_surface):
    """"100234-S0001" must never select "100234-S0001-3" while the exact option
    exists -- on this target those are two different member shares."""
    res = select_surface.select_option(SELECT_LOC, "100234-S0001", by="value")
    assert res.ok and _selected(select_surface) == "100234-S0001"


def test_no_matching_option_fails_and_names_what_was_offered(select_surface):
    """An eight-second timeout tells an operator nothing. The options do."""
    res = select_surface.select_option(SELECT_LOC, "NO_SUCH_OPTION", by="value")
    assert res.ok is False
    assert "NO_SUCH_OPTION" in res.message and "Regular Shares" in res.message
