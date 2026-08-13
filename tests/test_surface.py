"""
Direct tests of the Playwright web surface: element/readout indexing, locator
synthesis + resolution across candidates and frames, every checkpoint and
detector kind, coordinate clicks, attribute reads, and injected 500 handling.
"""
import urllib.request

import pytest

pytest.importorskip("playwright")

from bankcua.surface.web_playwright import WebSurface     # noqa: E402
from bankcua.schema import (Checkpoint, ConditionDetector, Locator,           # noqa
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
    # unlabeled input gets a label-proximity xpath ahead of structural css
    uloc = surf.locator_for_element(els[0])
    assert uloc.candidates[0].kind == LocatorKind.XPATH
    assert 'User ID' in uloc.candidates[0].value


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
