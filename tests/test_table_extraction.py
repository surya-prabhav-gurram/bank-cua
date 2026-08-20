"""
Grid reading, which is the one capability the round-1 core genuinely lacked.

A legacy console answers "what are this member's balances?" with a grid whose row
count varies per member, so no fixed set of scalar extract steps can express it.
The claim being defended here is not just "we can read a table" but "the SAME
recorded extraction reads it on a surface with a DOM and on one without" -- which
is the portability argument, and it is only worth anything if both readers agree
on the same page.

Runs offline against fixture markup shaped like MERIDIAN's shares grid, including
the two things that broke the first implementation against the live target: a
status cell rendered as two nodes, and a page footer with the same number of
columns as the grid.
"""
import pytest

pytest.importorskip("playwright")

from bankcua.schema import Locator, LocatorCandidate, LocatorKind
from bankcua.surface.accessibility import AccessibilitySurface
from bankcua.surface.web_playwright import WebSurface

# Shaped after MERIDIAN's member record: a header row, a status cell split into
# two nodes by a badge, and a footer key-bar with exactly as many columns as the
# grid -- which an implementation that matches on cell COUNT adopts as data.
GRID = """
<html><body>
<table border="0" cellpadding="3">
  <tr bgcolor="#dbe4ef" class="lbl">
    <td>Share ID</td><td>Type</td><td align="right">Balance</td><td>Status</td></tr>
  <tr><td>100234-S0001</td><td>Regular Shares</td>
      <td align="right">$1,499.00</td><td>HOLD <b>[HOLD]</b></td></tr>
  <tr><td>100234-S0070</td><td>Share Draft (Checking)</td>
      <td align="right">$2,241.55</td><td>OPEN</td></tr>
</table>
<br><br><br>
<table><tr><td>F3=Sign Off</td><td>F5=Main Menu</td>
           <td>F7=Member Inquiry</td><td>F12=Cancel</td></tr></table>
</body></html>
"""

GRID_LOC = Locator(description="shares grid", candidates=[
    LocatorCandidate(kind=LocatorKind.TEXT, value="Share ID",
                     reasoning="Addressed by its header cell -- the only handle a "
                               "surface without a DOM could also find.")])

EXPECTED_IDS = ["100234-S0001", "100234-S0070"]


@pytest.fixture(scope="module")
def reads():
    """Read the same fixture page through each surface, one at a time.

    Sequential rather than parallel because Playwright's sync API allows one
    driver per thread; holding both open at once fails before any assertion runs.
    """
    missing = Locator(description="absent grid", candidates=[
        LocatorCandidate(kind=LocatorKind.TEXT, value="No Such Header")])
    out = {}
    for name, cls in (("web", WebSurface), ("a11y", AccessibilitySurface)):
        s = cls("http://127.0.0.1:1", headless=True)
        s.start()
        try:
            s._page.set_content(GRID)
            out[name] = s.read(GRID_LOC, "table")
            out[name + ":missing"] = s.read(missing, "table")
        finally:
            s.stop()
    return out


def test_both_surfaces_read_the_same_grid_identically(reads):
    """The portability claim, as an assertion rather than an argument."""
    assert reads["web"].rows == reads["a11y"].rows, (
        f"surfaces disagree:\n web={reads['web'].rows}\na11y={reads['a11y'].rows}")


@pytest.mark.parametrize("surface", ["web", "a11y"])
def test_rows_are_keyed_on_the_grids_own_headers(reads, surface):
    """Keyed on headers, not positional indices: a vendor inserting a column
    between releases shifts every positional read one place left, and a balance
    silently read as a status is not a failure anyone notices."""
    rows = reads[surface].rows
    assert [r["Share ID"] for r in rows] == EXPECTED_IDS
    assert rows[0]["Balance"] == "$1,499.00"
    assert set(rows[0]) == {"Share ID", "Type", "Balance", "Status"}


@pytest.mark.parametrize("surface", ["web", "a11y"])
def test_a_multi_node_cell_is_not_dropped(reads, surface):
    """The HOLD row is the one row anybody actually cares about.

    Its status renders as two nodes (text plus a badge), which makes the band
    wider than the header on an a11y tree. An implementation matching on cell
    count drops the row entirely and reports a clean two-row grid -- losing the
    held share without raising anything at all."""
    assert "HOLD" in reads[surface].rows[0]["Status"]


@pytest.mark.parametrize("surface", ["web", "a11y"])
def test_the_footer_key_bar_is_not_adopted_as_data(reads, surface):
    """The footer has exactly as many columns as the grid, on purpose."""
    rows = reads[surface].rows
    assert len(rows) == 2
    assert not any("Sign Off" in " ".join(r.values()) for r in rows)


@pytest.mark.parametrize("surface", ["web", "a11y"])
def test_an_unreadable_target_fails_rather_than_returning_an_empty_grid(reads, surface):
    """Returning `ok=True, rows=[]` would let a run report success with an output
    the caller was promised and did not get. A failed read is recoverable; a
    silently empty one is a contract breach nothing downstream can detect."""
    res = reads[surface + ":missing"]
    assert res.ok is False and not res.rows


def test_rows_ride_a_separate_typed_channel_from_value(reads):
    """`value` keeps its str contract for every existing caller; a caller wanting
    rows cannot be handed a stringified grid it then has to re-parse."""
    res = reads["web"]
    assert isinstance(res.rows, list) and isinstance(res.rows[0], dict)
    assert isinstance(res.value, str)
