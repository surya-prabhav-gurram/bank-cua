"""
The conversational front door, and the boundary that makes it defensible.

Adding a chatbot puts a model back into a system built to keep models out of the
production decision loop. That is acceptable only because of where the boundary
sits: the model picks WHICH typed capability to call, and nothing else. So most
of what follows asserts the boundary rather than the behaviour -- that the front
door cannot reach past the API, cannot hold a secret, and cannot decide what it
is allowed to do.

The presenter tests are the other half: the result contract separates four things
a caller must not confuse, and the chatbot is the only part of this system most
people will ever look at. If the distinction dies in the rendering, it dies.
"""
import ast
import os

from bankcua.chat.presenter import present, render_outputs
from bankcua.chat.router import RuleRouter

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
CHAT_APP = os.path.join(ROOT, "bankcua", "chat", "app.py")

MANIFEST = [
    {"name": "meridian.member_lookup", "description": "look up a member",
     "input_schema": {"type": "object",
                      "properties": {"member_id": {}, "operator": {},
                                     "password": {}, "branch": {}},
                      "required": ["operator", "password", "branch", "member_id"]},
     "returns": {"shares": "table"}},
    {"name": "meridian.member_search", "description": "search by last name",
     "input_schema": {"type": "object",
                      "properties": {"last_name": {}, "operator": {},
                                     "password": {}, "branch": {}},
                      "required": ["operator", "password", "branch", "last_name"]},
     "returns": {"matches": "table"}},
    {"name": "meridian.transfer_funds", "description": "move money",
     "input_schema": {"type": "object",
                      "properties": {"member_id": {}, "from_share": {},
                                     "to_share": {}, "amount": {}, "memo": {},
                                     "operator": {}, "password": {}, "branch": {}},
                      "required": ["operator", "password", "branch", "member_id",
                                   "from_share", "to_share", "amount"]},
     "returns": {"confirmation": "string"}},
    {"name": "meridian.place_hold", "description": "restrict a share",
     "input_schema": {"type": "object",
                      "properties": {"member_id": {}, "share_id": {},
                                     "reason_code": {}, "notes": {},
                                     "operator": {}, "password": {}, "branch": {}},
                      "required": ["operator", "password", "branch", "member_id",
                                   "share_id", "reason_code"]},
     "returns": {"confirmation": "string"}},
]


# ---------------------------------------------------------------------------
# The seam, asserted structurally
# ---------------------------------------------------------------------------
def test_the_chatbot_cannot_reach_past_the_api():
    """It imports nothing from `bankcua` except its own router and presenter.

    This is what makes "the front door goes through the published API" a fact
    about the code rather than a claim in a docstring. If this module ever
    imports the engine, the catalog or the credential store, it can bypass every
    server-side check -- and it would still look fine in a demo.
    """
    tree = ast.parse(open(CHAT_APP).read())
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            reached.add(node.module)
        elif isinstance(node, ast.Import):
            reached.update(a.name for a in node.names)
    forbidden = {m for m in reached
                 if m.startswith("bankcua") or m in {"catalog", "service"}}
    # Relative imports inside the chat package appear with module=None or as
    # 'presenter'/'router'; anything naming bankcua directly is a breach.
    assert not forbidden, f"chat app reaches into the core directly: {forbidden}"
    assert "flask" in reached and "urllib.request" in reached


def test_the_chatbot_never_names_a_password():
    """It sends an operator ALIAS. If a password appears in this module at all,
    the credential store has been routed around."""
    source = open(CHAT_APP).read()
    lowered = source.lower()
    assert '"password"' not in lowered and "'password'" not in lowered


# ---------------------------------------------------------------------------
# Routing: what it must refuse to invent
# ---------------------------------------------------------------------------
def test_routes_a_balance_question_to_the_lookup_capability():
    r = RuleRouter().route("what are the balances for member 100234?", MANIFEST)
    assert r.capability_id == "meridian.member_lookup"
    assert r.params["member_id"] == "100234"
    assert r.ok


def test_never_invents_a_member_number_and_asks_instead():
    """The one thing a router must not do is guess an account identifier.

    Asking is the correct behaviour, not a limitation: a fabricated member
    number is a request against somebody else's account."""
    r = RuleRouter().route("what is the balance?", MANIFEST)
    assert r.capability_id == "meridian.member_lookup"
    assert "member_id" in r.missing and not r.ok


def test_never_invents_an_amount_for_a_transfer():
    r = RuleRouter().route("transfer from 100234-S0070 to 100234-S0001-3", MANIFEST)
    assert r.capability_id == "meridian.transfer_funds"
    assert "amount" in r.missing and not r.ok


def test_a_share_id_does_not_donate_a_member_number_it_never_named():
    """"100234-S0070" contains six digits that are not a member the user asked
    about -- unless it is the only thing they gave us."""
    r = RuleRouter().route("place a fraud hold on 100234-S0070", MANIFEST)
    assert r.capability_id == "meridian.place_hold"
    assert r.params["share_id"] == "100234-S0070"
    assert r.params["member_id"] == "100234"
    assert r.params["reason_code"] == "FRAUD"


def test_credentials_are_never_treated_as_missing_arguments():
    """operator/password/branch are supplied by the service from the operator's
    identity, so the chatbot must not ask a human for them."""
    r = RuleRouter().route("balances for member 100234", MANIFEST)
    assert not ({"operator", "password", "branch"} & set(r.missing))


def test_an_unrecognised_request_says_so_rather_than_guessing():
    r = RuleRouter().route("what is the weather", MANIFEST)
    assert r.capability_id is None
    assert "could not tell" in r.unmatched_reason


def test_a_capability_absent_from_the_manifest_is_never_routed_to():
    """The catalog is the entire action space. A capability this deployment does
    not publish must be unreachable, not merely unlikely."""
    without_transfers = [c for c in MANIFEST
                         if c["name"] != "meridian.transfer_funds"]
    r = RuleRouter().route("transfer 50 from 100234-S0070 to 100234-S0001-3",
                           without_transfers)
    assert r.capability_id != "meridian.transfer_funds"


# ---------------------------------------------------------------------------
# Presentation: the contract has to survive contact with English
# ---------------------------------------------------------------------------
def test_a_business_outcome_does_not_read_as_a_malfunction():
    text = present({"status": "business_outcome", "outputs": {},
                    "business_outcome": {"code": "NO_SEARCH_MATCHES"}})
    assert "No member records matched" in text
    lowered = text.lower()
    assert "error" not in lowered and "went wrong" not in lowered
    assert "failed" not in lowered


def test_a_refusal_says_nothing_was_submitted_and_what_would_change_it():
    text = present({"status": "refused", "outputs": {},
                    "refusal": {"code": "DUAL_CONTROL_REQUIRED",
                                "requirement": "an independent second approver",
                                "reason": "over threshold"}})
    assert "nothing was submitted" in text.lower()
    assert "independent second approver" in text
    assert "failure" not in text.lower()


def test_a_failure_is_the_only_thing_that_reads_as_broken():
    text = present({"status": "failure", "outputs": {},
                    "failure": {"code": "SESSION_TIMED_OUT", "step_index": 4,
                                "expected": "the member record",
                                "observed": "session expiry page"}})
    assert "step 4" in text
    assert "investigat" in text.lower()
    assert "SESSION_TIMED_OUT" in text


def test_an_escalation_says_it_is_paused_and_not_completed():
    text = present({"status": "escalated", "outputs": {},
                    "intervention_id": "replay-x-step9"})
    assert "replay-x-step9" in text
    assert "nothing was completed" in text.lower()


def test_the_four_statuses_never_render_identically():
    """Different states must be distinguishable to the person reading them --
    that is the entire reason the contract has four of them."""
    rendered = {
        present({"status": "success", "outputs": {"confirmation": "CN1"}}),
        present({"status": "business_outcome", "outputs": {},
                 "business_outcome": {"code": "INSUFFICIENT_FUNDS"}}),
        present({"status": "refused", "outputs": {},
                 "refusal": {"code": "VALUE_LIMIT_EXCEEDED", "requirement": "x"}}),
        present({"status": "failure", "outputs": {},
                 "failure": {"code": "ACTION_FAILED"}}),
    }
    assert len(rendered) == 4


def test_an_outcome_that_carries_data_still_shows_it():
    """A legitimate non-success can still return what it does know."""
    text = present({"status": "business_outcome",
                    "outputs": {"member_name": "Lovelace, Ada"},
                    "business_outcome": {"code": "SUPERVISOR_OVERRIDE_REQUIRED",
                                         "outputs_surfaced": ["member_name"]}})
    assert "Lovelace, Ada" in text


def test_a_table_renders_as_a_table_not_as_a_blob():
    """Typed rows exist so a person can read them. Flattening the grid into a
    dict dump gives up the reason for having them."""
    text = render_outputs({"shares": [
        {"Share ID": "100234-S0001", "Balance": "$1,499.00", "Status": "HOLD"},
        {"Share ID": "100234-S0070", "Balance": "$2,241.55", "Status": "OPEN"}]})
    lines = text.splitlines()
    assert lines[1].split() == ["Share", "ID", "Balance", "Status"]
    assert "100234-S0001" in text and "$2,241.55" in text
    # column order preserved, not alphabetised
    assert text.index("Share ID") < text.index("Balance") < text.index("Status")


def test_a_share_id_missing_its_member_is_asked_for_rather_than_repaired():
    """A model asked to move money out of "100234-S0070" has returned
    `from_share: "S0070"` -- the suffix alone, which identifies no account.

    Passing that through would let the surface's prefix matching select whichever
    option happened to start with it, on a live transfer. Asking is the only safe
    answer, and it is the same rule the deterministic router follows.
    """
    from bankcua.chat.router import _drop_malformed_share_ids
    cleaned, malformed = _drop_malformed_share_ids(
        {"member_id": "100234", "from_share": "S0070",
         "to_share": "100234-S0001", "amount": "25"})
    assert "from_share" not in cleaned and "from_share" in malformed
    assert cleaned["to_share"] == "100234-S0001", "a valid share id must survive"
    assert cleaned["amount"] == "25"


def test_share_ids_pass_through_when_no_member_is_known():
    """With no member_id to check against there is nothing to validate, and
    inventing a rule would reject legitimate input."""
    from bankcua.chat.router import _drop_malformed_share_ids
    cleaned, malformed = _drop_malformed_share_ids({"share_id": "100987-S0001-4"})
    assert not malformed and cleaned["share_id"] == "100987-S0001-4"


def test_a_number_that_cannot_be_a_member_id_is_rejected_with_a_reason():
    """"I still need a member number" reads as though none was given.

    The user gave one; it just cannot be a member number. Reporting it as
    missing invites them to retype the same thing. Meridian numbers are six
    digits and its search matches on SUBSTRING, so a shorter number silently
    resolves to a different member -- which makes this the cheapest of the four
    places that danger is caught: before a browser, before the API, before the
    condition taxonomy.
    """
    r = RuleRouter().route("balances for member 1002", MANIFEST)
    assert r.capability_id == "meridian.member_lookup"
    assert "member_id" in r.missing
    assert "not a member number" in r.rejected["member_id"]
    assert "six digits" in r.rejected["member_id"]


def test_a_valid_member_number_is_never_rejected():
    r = RuleRouter().route("balances for member 100234", MANIFEST)
    assert r.params["member_id"] == "100234" and not r.rejected


def test_a_sentence_with_no_number_at_all_asks_plainly():
    """Nothing was offered, so there is nothing to explain -- the reply should
    not manufacture a complaint about input that was never given."""
    r = RuleRouter().route("what is the balance", MANIFEST)
    assert "member_id" in r.missing and not r.rejected


def test_a_share_id_is_not_mistaken_for_a_bad_member_number():
    """"100234-S0070" contains digit runs of other lengths; none of them is a
    rejected member number, because the share id is stripped first."""
    r = RuleRouter().route("place a fraud hold on 100234-S0070", MANIFEST)
    assert not r.rejected
