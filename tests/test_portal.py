"""
The signed-in console: one sign-in, two tabs, and one subject behind both.

What is worth asserting here is not that a login form works. It is that the
console narrows by WHO IS SIGNED IN on the server, before anything is
serialised -- because a page that fetched every run and hid the other members'
rows in CSS would be one "view source" away from disclosing them, and it would
look identical in a demo.

So: a member gets a different page, a smaller run list, and a 404 on evidence
that is not theirs. And the portal itself is only a door -- the refusals that
matter live in the API (tests/test_session_authz.py) and would still be there if
this file were deleted.
"""
import json
import os

import pytest

from bankcua.auth import (PrincipalStore, SessionAuthority, SessionSigner,
                          SESSION_COOKIE, hash_password)

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
CATALOG = os.path.join(ROOT, "capabilities", "meridian")


def _run(root, run_id, capability, status="success", principal=None,
         outputs=None):
    """One run on disk: a summary, and optionally the subject who asked for it."""
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(json.dumps(
        {"status": status, "capability_id": capability, "version": "1.0.0",
         "steps_executed": 3, "duration_s": 0.5, "outputs": outputs or {}}))
    (run_dir / "step00.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    if principal:
        (run_dir / "principal.json").write_text(json.dumps(principal))
    return run_id


@pytest.fixture
def evidence(tmp_path):
    root = tmp_path / "ev"
    root.mkdir()
    ids = {
        "mine": _run(root, "meridian.member_lookup-20260820-000001-000",
                     "meridian.member_lookup",
                     principal={"username": "100234", "role": "member",
                                "kind": "member", "member_id": "100234"},
                     outputs={"shares": [{"Share ID": "100234-S0070",
                                          "Balance": "$120.00"}]}),
        "theirs": _run(root, "meridian.member_lookup-20260820-000002-000",
                       "meridian.member_lookup",
                       principal={"username": "100987", "role": "member",
                                  "kind": "member", "member_id": "100987"},
                       outputs={"shares": [{"Share ID": "100987-S0001",
                                            "Balance": "$9,900.00"}]}),
        "operator": _run(root, "meridian.place_hold-20260820-000003-000",
                         "meridian.place_hold", status="escalated"),
    }
    return root, ids


@pytest.fixture
def portal(tmp_path, evidence):
    from bankcua.portal.app import create_app
    root, _ids = evidence
    principals = tmp_path / "principals.json"
    principals.write_text(json.dumps({
        "super1": {"kind": "staff", "role": "supervisor", "acts_as": "super1",
                   "password_hash": hash_password("password", rounds=1000)},
        "teller1": {"kind": "staff", "role": "teller", "acts_as": "teller1",
                    "password_hash": hash_password("password", rounds=1000)},
        "100234": {"kind": "member", "role": "member", "member_id": "100234",
                   "display_name": "Member 100234",
                   "password_hash": hash_password("member123", rounds=1000)},
    }))
    authority = SessionAuthority(store=PrincipalStore(str(principals)),
                                 signer=SessionSigner(b"portal-test-key"))
    return create_app(catalog_dir=CATALOG, evidence_dirs=(str(root),),
                      handoff_dir=str(tmp_path / "handoffs"),
                      principals_path=str(principals),
                      session_key_path=str(tmp_path / "session.key"),
                      session_authority=authority)


def _sign_in(client, username, password):
    return client.post("/login", data={"username": username,
                                       "password": password})


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------
def test_nothing_is_reachable_without_signing_in(portal):
    client = portal.test_client()
    for path in ("/", "/dashboard/", "/assistant/", "/dashboard/api/runs"):
        r = client.get(path)
        assert r.status_code == 302, path
        assert "/login" in r.headers["Location"], path


def test_a_wrong_password_says_nothing_about_which_sign_ins_exist(portal):
    client = portal.test_client()
    wrong = _sign_in(client, "100234", "not-it")
    unknown = _sign_in(client, "100999", "not-it")
    assert wrong.status_code == 401 and unknown.status_code == 401
    assert b"do not match" in wrong.data and b"do not match" in unknown.data
    assert SESSION_COOKIE not in (wrong.headers.get("Set-Cookie") or "")


def test_signing_in_sets_a_session_the_page_cannot_read(portal):
    client = portal.test_client()
    r = _sign_in(client, "teller1", "password")
    assert r.status_code == 302 and r.headers["Location"] == "/"
    cookie = r.headers["Set-Cookie"]
    assert SESSION_COOKIE in cookie
    # A token JavaScript can read is a token an injected script can post
    # somewhere, and SameSite is what stops a cross-site form acting as this
    # session.
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_the_sign_in_page_is_not_an_open_redirect(portal):
    """A real bank console URL is a credible phishing landing spot precisely
    because it is real."""
    client = portal.test_client()
    r = client.post("/login?next=https://elsewhere.example/",
                    data={"username": "teller1", "password": "password"})
    assert r.headers["Location"] == "/"


def test_repeated_failures_are_held_off(portal):
    """Member usernames ARE member numbers, so an unthrottled form is an online
    guess against a known, enumerable account list."""
    client = portal.test_client()
    for _ in range(5):
        assert _sign_in(client, "100234", "wrong").status_code == 401
    assert _sign_in(client, "100234", "wrong").status_code == 429
    # ...and the correct password does not get a free pass through the lockout
    assert _sign_in(client, "100234", "member123").status_code == 429


def test_signing_out_ends_the_session(portal):
    client = portal.test_client()
    _sign_in(client, "teller1", "password")
    assert client.get("/").status_code == 200
    client.post("/logout")
    assert client.get("/").status_code == 302


def test_the_shell_offers_exactly_two_tabs(portal):
    client = portal.test_client()
    _sign_in(client, "teller1", "password")
    page = client.get("/").data.decode()
    assert 'src="/dashboard/"' in page and 'src="/assistant/"' in page
    assert ">Dashboard<" in page and ">Assistant<" in page
    assert "Sign out" in page


def test_the_mounted_tabs_build_their_urls_from_the_mount_point(portal):
    """Both tabs run in a frame under a path. An absolute /api/... from inside
    the frame hits the PORTAL and comes back as a sign-in page -- which looks,
    from the tab, exactly like the API having gone away."""
    client = portal.test_client()
    _sign_in(client, "teller1", "password")
    assert 'const P="/dashboard";' in client.get("/dashboard/").data.decode()
    assert 'const P="/assistant";' in client.get("/assistant/").data.decode()


# ---------------------------------------------------------------------------
# Two audiences
# ---------------------------------------------------------------------------
def test_staff_get_the_operator_console(portal):
    client = portal.test_client()
    _sign_in(client, "teller1", "password")
    page = client.get("/dashboard/").data.decode()
    assert "Capability catalog" in page and "Run history" in page
    assert "Teller" in client.get("/dashboard/api/me").get_json()["role"].title()


def test_a_member_gets_a_different_page_not_the_operator_one_with_panels_hidden(
        portal):
    """The operator page is built around a catalog, an escalation queue and
    every run on the host. A version of it that merely HID those would still
    have fetched them."""
    client = portal.test_client()
    _sign_in(client, "100234", "member123")
    page = client.get("/dashboard/").data.decode()
    assert "My accounts" in page and "My record" in page
    assert "Capability catalog" not in page
    assert "Paused — waiting for a person" not in page


def test_a_member_sees_only_their_own_runs(portal, evidence):
    _root, ids = evidence
    client = portal.test_client()
    _sign_in(client, "100234", "member123")
    shown = {r["run_id"] for r in client.get(
        "/dashboard/api/runs").get_json()["shown"]}
    assert shown == {ids["mine"]}
    assert ids["theirs"] not in shown
    # A run with NO recorded subject is an operator-driven one, over whichever
    # member the operator was working on. Failing closed there is the point.
    assert ids["operator"] not in shown


def test_a_member_cannot_open_another_members_run_by_guessing_its_id(portal,
                                                                     evidence):
    """Run ids are a capability name and a timestamp, so they are guessable --
    and the routes behind them serve screenshots of member accounts."""
    _root, ids = evidence
    client = portal.test_client()
    _sign_in(client, "100234", "member123")
    assert client.get(f"/dashboard/api/runs/{ids['mine']}").status_code == 200
    for other in (ids["theirs"], ids["operator"]):
        # 404 rather than 403: confirming that the run exists is itself a
        # disclosure.
        assert client.get(f"/dashboard/api/runs/{other}").status_code == 404
        assert client.get(
            f"/dashboard/api/runs/{other}/evidence/step00.png").status_code == 404
    assert client.get(
        f"/dashboard/api/runs/{ids['mine']}/evidence/step00.png").status_code == 200


def test_staff_still_see_every_run(portal, evidence):
    _root, ids = evidence
    client = portal.test_client()
    _sign_in(client, "super1", "password")
    shown = {r["run_id"] for r in client.get(
        "/dashboard/api/runs").get_json()["shown"]}
    assert shown == set(ids.values())


def test_a_members_own_record_comes_from_the_evidence_not_a_second_store(portal):
    """Same rule as the rest of this console: what a member sees is what the
    automation actually read off Meridian, and when."""
    client = portal.test_client()
    _sign_in(client, "100234", "member123")
    body = client.get("/dashboard/api/me/details").get_json()
    assert body["member_id"] == "100234"
    assert body["outputs"]["shares"][0]["Share ID"] == "100234-S0070"
    assert body["as_of_run"]


def test_the_operator_queue_is_not_shown_to_a_member(portal):
    """A paused run's reason text and screenshot describe whichever member the
    operator was working on."""
    client = portal.test_client()
    _sign_in(client, "100234", "member123")
    assert client.get("/dashboard/api/interventions").get_json() == []


def test_taking_control_of_a_paused_session_is_supervisor_work(portal):
    """The whole point of the pause is that a supervisor finishes the action on
    the live session. A teller attaching would make the escalation ceremonial;
    a member attaching would be a stranger on a banking session."""
    client = portal.test_client()
    _sign_in(client, "teller1", "password")
    assert client.post(
        "/dashboard/api/interventions/whatever/console").status_code == 403
    client.post("/logout")
    _sign_in(client, "100234", "member123")
    assert client.post(
        "/dashboard/api/interventions/whatever/console").status_code == 403


def test_the_assistant_is_reachable_signed_in_and_not_otherwise(portal):
    client = portal.test_client()
    assert client.get("/assistant/").status_code == 302
    _sign_in(client, "100234", "member123")
    page = client.get("/assistant/").data.decode()
    assert "Meridian assistant" in page
