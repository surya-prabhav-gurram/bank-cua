"""
Every locator strategy the schema declares must actually resolve.

`LocatorKind` is the vocabulary an artifact uses to say "find this control".
Several members are never emitted by the current compiler -- the mock target has
no test IDs, no alt text and no title attributes, because legacy enterprise apps
don't -- which makes them look like dead schema surface. They are not: the
resolver honours them, so an artifact hand-authored for a different app, or
emitted by a future surface, works without a schema change.

This test is what makes that claim checkable rather than a promise. If a kind is
ever added to the enum without a resolution path, it fails here.
"""
import pytest

pytest.importorskip("playwright")

from bankcua.schema import Locator, LocatorCandidate, LocatorKind
from bankcua.surface.web_playwright import WebSurface

# One element per strategy, in the kind of non-semantic markup this system targets.
PAGE = """
<html><body>
  <table><tr>
    <td><button id="b1" aria-label="Post Journal">go</button></td>
    <td><label for="i1">Member ID</label><input id="i1" type="text"></td>
    <td><input id="i2" type="text" placeholder="Search members"></td>
    <td><a href="/x">Open New Sub-Account</a></td>
    <td><img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" alt="Print statement"></td>
    <td><span title="Locked account">!</span></td>
    <td><button data-testid="confirm-btn">ok</button></td>
    <td><span class="bal">$1.00</span></td>
  </tr>
  <tr><td>User ID</td><td><input id="i3" type="text"></td></tr>
  <tr><td>Savings</td><td>$4,213.55</td></tr>
  </table>
</body></html>
"""

CASES = {
    LocatorKind.ROLE:        LocatorCandidate(kind=LocatorKind.ROLE, role="button",
                                              value="Post Journal"),
    LocatorKind.LABEL:       LocatorCandidate(kind=LocatorKind.LABEL, value="Member ID"),
    LocatorKind.PLACEHOLDER: LocatorCandidate(kind=LocatorKind.PLACEHOLDER,
                                              value="Search members"),
    LocatorKind.TEXT:        LocatorCandidate(kind=LocatorKind.TEXT,
                                              value="Open New Sub-Account"),
    LocatorKind.ALT_TEXT:    LocatorCandidate(kind=LocatorKind.ALT_TEXT,
                                              value="Print statement"),
    LocatorKind.TITLE:       LocatorCandidate(kind=LocatorKind.TITLE,
                                              value="Locked account"),
    LocatorKind.TEST_ID:     LocatorCandidate(kind=LocatorKind.TEST_ID,
                                              value="confirm-btn"),
    # No accessible name, no <label for> -- addressable only by proximity. This
    # is the legacy default, and the one strategy that ports to a non-DOM surface.
    LocatorKind.NEAR_LABEL:  LocatorCandidate(kind=LocatorKind.NEAR_LABEL,
                                              value="User ID", role="textbox"),
    LocatorKind.CSS:         LocatorCandidate(kind=LocatorKind.CSS, value="span.bal"),
    LocatorKind.XPATH:       LocatorCandidate(kind=LocatorKind.XPATH,
                                              value='//span[@class="bal"]'),
    # COORDINATES is resolved by the caller (mouse click), not by a DOM query --
    # it is the screenshot-based floor for surfaces with no queryable tree at all.
    LocatorKind.COORDINATES: None,
}


@pytest.fixture(scope="module")
def surface():
    s = WebSurface("http://127.0.0.1:1", headless=True)
    s.start()
    s._page.set_content(PAGE)
    yield s
    s.stop()


def test_every_declared_kind_has_a_case():
    """The enum and this test cannot drift apart."""
    assert set(CASES) == set(LocatorKind)


@pytest.mark.parametrize("kind", [k for k, v in CASES.items() if v is not None])
def test_declared_kind_resolves_to_an_element(surface, kind):
    loc = Locator(description=f"probe:{kind.value}", candidates=[CASES[kind]])
    resolved, index = surface._resolve(loc)
    assert resolved is not None, f"{kind.value} did not resolve"
    assert index == 0


def test_coordinates_resolve_through_the_mouse_path(surface):
    loc = Locator(description="probe:coords",
                  candidates=[LocatorCandidate(kind=LocatorKind.COORDINATES,
                                               value="0.5,0.5")])
    resolved, _index = surface._resolve(loc)
    assert isinstance(resolved, str) and resolved.startswith("__coords__:")
    assert surface.click(loc).ok is True


def test_near_label_resolves_a_read_only_value_not_only_a_control(surface):
    """Adjacency has two shapes and a legacy form has both.

    "The box next to 'User ID'" is writeable; "the amount next to 'Savings'" is
    not. Both are the same recorded intent, and the non-DOM surface resolves
    either, so this one must too -- otherwise every extract step silently
    resolves one candidate further down its list on the surface it was recorded
    on, and the primary strategy is decorative.
    """
    loc = Locator(description="probe:readonly",
                  candidates=[LocatorCandidate(kind=LocatorKind.NEAR_LABEL,
                                               value="Savings")])
    resolved, index = surface._resolve(loc)
    assert resolved is not None and index == 0
    assert surface.read(loc).value == "$4,213.55"


def test_near_label_still_prefers_the_control_when_the_row_has_one(surface):
    """The read-only path must not cost us the writeable one: a row holding an
    input still resolves to the input, so a fill targets the box and not the cell
    around it."""
    loc = Locator(description="probe:writeable",
                  candidates=[LocatorCandidate(kind=LocatorKind.NEAR_LABEL,
                                               value="User ID", role="textbox")])
    assert surface.fill(loc, "operator").ok is True
    assert surface.read(loc, "value").value == "operator"


def test_first_resolvable_candidate_wins_and_reports_its_index(surface):
    """The fallback ladder in one assertion: a stale primary is skipped and the
    index that actually resolved is reported, which is what feeds drift telemetry."""
    loc = Locator(description="probe:ladder", candidates=[
        LocatorCandidate(kind=LocatorKind.ROLE, role="button", value="No Such Button"),
        LocatorCandidate(kind=LocatorKind.CSS, value="span.bal"),
    ])
    resolved, index = surface._resolve(loc)
    assert resolved is not None and index == 1
