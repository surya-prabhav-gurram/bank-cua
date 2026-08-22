"""
The signed-in console: one sign-in, two tabs, and one subject behind both.

What is worth asserting here is not that a login form works. It is who the door
lets through, and that it decides on the SERVER -- a console that admitted
anyone and then hid the panels in CSS would be one "view source" away from
disclosing them, and would look identical in a demo.

This console admits the institution's own MERIDIAN operators and nobody else.
The principal fixture below still contains a member entry, deliberately: the
guarantee has to hold against a hand-edited principal file, not merely against a
login page that has one form on it.

The portal itself is only a door -- the refusals that matter live in the API
(tests/test_session_authz.py) and would still be there if this file were
deleted.
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
        "by_teller": _run(root, "meridian.member_lookup-20260820-000001-000",
                          "meridian.member_lookup",
                          principal={"username": "teller1", "role": "teller",
                                     "runs_as": "teller1"},
                          outputs={"shares": [{"Share ID": "100234-S0070",
                                               "Balance": "$120.00"}]}),
        "by_super": _run(root, "meridian.member_lookup-20260820-000002-000",
                         "meridian.member_lookup",
                         principal={"username": "super1", "role": "supervisor",
                                    "runs_as": "super1"},
                         outputs={"shares": [{"Share ID": "100987-S0001",
                                              "Balance": "$9,900.00"}]}),
        # No recorded subject: a run made from the CLI, before anyone signed in.
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
    wrong = _sign_in(client, "teller1", "not-it")
    unknown = _sign_in(client, "nobody1", "not-it")
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
    """The operator names are few and guessable, so a form that accepts a
    password unthrottled is an online guess against a known account list."""
    client = portal.test_client()
    for _ in range(5):
        assert _sign_in(client, "teller1", "wrong").status_code == 401
    assert _sign_in(client, "teller1", "wrong").status_code == 429
    # ...and the correct password does not get a free pass through the lockout
    assert _sign_in(client, "teller1", "password").status_code == 429


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


def test_a_member_entry_in_the_store_cannot_sign_in(portal):
    """The console has no member form -- but that is not what enforces it.

    `PrincipalStore.get` refuses any principal whose role is not an operator
    role, so a member left in (or added to) the principal file is turned away
    with the same message as a wrong password, and no cookie is set.
    """
    client = portal.test_client()
    r = _sign_in(client, "100234", "member123")
    assert r.status_code == 401
    assert b"do not match" in r.data
    assert SESSION_COOKIE not in (r.headers.get("Set-Cookie") or "")


def test_the_login_page_offers_operators_only(portal):
    """The dropdown is built from the store, and skips what it cannot resolve,
    so a member entry is invisible rather than offered and then refused."""
    page = portal.test_client().get("/login").data.decode()
    assert "super1" in page and "teller1" in page
    assert "100234" not in page
    assert "Member number" not in page


def test_every_operator_sees_every_run(portal, evidence):
    """The run history is the institution's own audit trail. A teller who could
    not see a supervisor's runs could not review one either."""
    _root, ids = evidence
    client = portal.test_client()
    _sign_in(client, "teller1", "password")
    shown = {r["run_id"] for r in client.get(
        "/dashboard/api/runs").get_json()["shown"]}
    assert shown == set(ids.values())

    client.post("/logout")
    _sign_in(client, "super1", "password")
    assert {r["run_id"] for r in client.get(
        "/dashboard/api/runs").get_json()["shown"]} == set(ids.values())


def test_evidence_is_served_only_for_runs_that_produced_it(portal, evidence):
    """Run ids are a capability name and a timestamp, so they are guessable, and
    these routes serve screenshots of member accounts. Being signed in is not
    permission to name an arbitrary path."""
    _root, ids = evidence
    client = portal.test_client()
    _sign_in(client, "teller1", "password")
    good = ids["by_teller"]
    assert client.get(f"/dashboard/api/runs/{good}").status_code == 200
    assert client.get(
        f"/dashboard/api/runs/{good}/evidence/step00.png").status_code == 200
    assert client.get(
        "/dashboard/api/runs/no-such-run-00000000-000").status_code == 404
    assert client.get(
        f"/dashboard/api/runs/{good}/evidence/summary.json").status_code == 404


def test_taking_control_of_a_paused_session_is_supervisor_work(portal):
    """The whole point of the pause is that a supervisor finishes the action on
    the live session. A teller attaching would make the escalation ceremonial."""
    client = portal.test_client()
    _sign_in(client, "teller1", "password")
    assert client.post(
        "/dashboard/api/interventions/whatever/console").status_code == 403


def test_the_assistant_is_reachable_signed_in_and_not_otherwise(portal):
    client = portal.test_client()
    assert client.get("/assistant/").status_code == 302
    _sign_in(client, "teller1", "password")
    page = client.get("/assistant/").data.decode()
    assert "Meridian assistant" in page
