"""
The vendor condition libraries are data, so the invariants that make them safe
have to be enforced here rather than assumed by the engine that reads them.

Three failure modes are worth catching, and none of them raise on their own:
a library that does not parse (replay silently loses ALL condition detection),
a detector with no text (matches nothing, forever, invisibly), and a generic
condition ordered ahead of a specific one (the generic one swallows it, and the
caller gets TRANSACTION_REJECTED where INSUFFICIENT_FUNDS was the real answer).
"""
import os

import pytest
import yaml

from bankcua.knowledge import (KNOWLEDGE_DIR, VendorLibraryError,
                               available_vendors, conditions_for)
from bankcua.schema import ConditionClass

VENDORS = available_vendors()


def test_repo_ships_at_least_the_two_vendors_it_documents():
    assert {"corebank", "meridian"} <= set(VENDORS)


@pytest.mark.parametrize("vendor", VENDORS)
def test_every_shipped_library_loads_and_is_non_empty(vendor):
    conds = conditions_for(vendor)
    assert conds, f"{vendor} library loaded but is empty"


@pytest.mark.parametrize("vendor", VENDORS)
def test_every_detector_can_actually_match_something(vendor):
    """A detector with an empty value matches nothing and reports nothing.

    It looks like coverage in the file and behaves like absence at runtime, which
    is the worst combination available."""
    for c in conditions_for(vendor):
        assert c.detector.value.strip(), f"{vendor}/{c.code} has an empty detector"


@pytest.mark.parametrize("vendor", VENDORS)
def test_recoverable_conditions_declare_a_recovery(vendor):
    """`recoverable` without a `recovery` is a contradiction: the engine would
    classify it as handleable and then have nothing to run, converting it into a
    recovery failure — a hard error wearing a recoverable label."""
    for c in conditions_for(vendor):
        if c.klass == ConditionClass.RECOVERABLE:
            assert c.recovery is not None, f"{vendor}/{c.code} is recoverable with no recovery"


@pytest.mark.parametrize("vendor", VENDORS)
def test_codes_are_unique_within_a_vendor(vendor):
    codes = [c.code for c in conditions_for(vendor)]
    assert len(codes) == len(set(codes)), f"{vendor} has duplicate condition codes"


def test_specific_conditions_precede_the_generic_page_that_would_swallow_them():
    """Replay acts on the FIRST match, so declaration order is behaviour.

    On Meridian an overdraw and a generic field rejection are both HTTP 400.
    If TRANSACTION_REJECTED were declared first it would match the overdraw page
    too, and the caller would be told "rejected as entered" instead of
    "insufficient funds" — a strictly less actionable answer, produced silently.
    """
    codes = [c.code for c in conditions_for("Meridian")]
    assert codes.index("INSUFFICIENT_FUNDS") < codes.index("TRANSACTION_REJECTED")


def test_supervisor_override_is_an_outcome_not_a_failure():
    """The application's own RBAC declining a teller is an ANSWER.

    Classifying it as a hard failure would page an engineer every time an
    authorisation boundary worked correctly."""
    c = next(c for c in conditions_for("Meridian")
             if c.code == "SUPERVISOR_OVERRIDE_REQUIRED")
    assert c.klass == ConditionClass.BUSINESS_OUTCOME


def test_session_timeout_is_never_silently_recoverable():
    """Re-authenticating mid-transaction and resuming is where double-execution
    lives: we cannot know whether the step that timed out landed. This must stay
    a hard failure for every vendor, so the caller decides."""
    for vendor in VENDORS:
        for c in conditions_for(vendor):
            if "TIME" in c.code or "TIMED" in c.code or "SESSION" in c.code:
                assert c.klass == ConditionClass.HARD_FAILURE, (
                    f"{vendor}/{c.code} would let replay resume across a session "
                    f"boundary it cannot reason about")


def test_a_malformed_library_raises_instead_of_loading_empty(tmp_path, monkeypatch):
    """Failing closed at load, because the alternative is invisible.

    A library that silently loads as empty gives a replay with NO condition
    detection: every business outcome becomes an unexplained checkpoint failure,
    and nothing in the result says the taxonomy is missing."""
    import bankcua.knowledge as k
    bad = tmp_path / "brokenvendor.yaml"
    bad.write_text(yaml.safe_dump(
        {"vendor": "BrokenVendor",
         "conditions": [{"code": "X", "klass": "not_a_real_class",
                         "detector": {"kind": "text_present", "value": "x"}}]}))
    monkeypatch.setattr(k, "KNOWLEDGE_DIR", str(tmp_path))
    k._load.cache_clear()
    with pytest.raises(VendorLibraryError):
        k.conditions_for("BrokenVendor")
    k._load.cache_clear()


def test_library_files_are_documented_for_the_people_who_maintain_them():
    """These files are edited by whoever knows the vendor's screens, not by
    whoever knows this engine. A bare data file with no explanation of what
    `klass` does is not reviewable by that audience."""
    for vendor in VENDORS:
        text = open(os.path.join(KNOWLEDGE_DIR, f"{vendor}.yaml")).read()
        head = text.split("vendor:")[0]
        assert len(head.strip()) > 200, f"{vendor}.yaml has no explanatory header"


def test_conditions_for_returns_copies_the_caller_may_mutate():
    """Tenant overrides remap detector text in place; the shared library must not
    be reachable from that edit or one tenant's rebrand changes every tenant's."""
    a = conditions_for("Meridian")
    a[0].detector.value = "MUTATED"
    assert conditions_for("Meridian")[0].detector.value != "MUTATED"
