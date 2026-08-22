"""
Signing on happens at the door, once, and then stops being askable.

Two halves of one decision, and each is worthless without the other. The console
establishes the operator's session on the target when a person signs in, so
nobody types an operator password and nobody has to remember to run a capability
first. And BECAUSE it happens there, `meridian.signon` is withheld from every
signed-in caller: it is not in the manifest a chatbot routes over, and /invoke
refuses it outright. A capability whose only remaining effect is to redo what the
sign-in already did is one that can only ever be called by mistake.

The tests below are mostly about what CANNOT be asked for, which is the same
shape as the rest of this suite: every refusal here is answered before a browser
is launched.
"""
import json
import os

import pytest

from bankcua.auth import (PrincipalStore, SessionAuthority, SessionSigner,
                          hash_password)
from bankcua.chat.router import RuleRouter
from bankcua.portal.app import create_app as create_portal
from bankcua.safety.credentials import OperatorIdentity, StaticCredentialStore
from bankcua.service import create_app as create_api

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
CATALOG = os.path.join(ROOT, "capabilities", "meridian")
SIGNON = "meridian.signon"

BRANCH = {"branch": "MAIN-001"}
CREDS = StaticCredentialStore({
    "teller1": OperatorIdentity("teller1", "teller", {"password": "pw"}, BRANCH),
    "super1": OperatorIdentity("super1", "supervisor", {"password": "pw"}, BRANCH),
})


def _skip_without_catalog():
    if not os.path.isdir(CATALOG) or not os.listdir(CATALOG):
        pytest.skip("meridian capabilities not recorded")


@pytest.fixture
def principals(tmp_path):
    path = tmp_path / "principals.json"
    path.write_text(json.dumps({
        "super1": {"kind": "staff", "role": "supervisor", "acts_as": "super1",
                   "display_name": "Supervisor (super1)",
                   "password_hash": hash_password("password", rounds=1000)},
        "teller1": {"kind": "staff", "role": "teller", "acts_as": "teller1",
                    "display_name": "Teller (teller1)",
                    "password_hash": hash_password("password", rounds=1000)},
        "100234": {"kind": "member", "role": "member", "member_id": "100234",
                   "password_hash": hash_password("member123", rounds=1000)},
    }))
    return str(path)


@pytest.fixture
def authority(principals):
    return SessionAuthority(store=PrincipalStore(principals),
                            signer=SessionSigner(b"test-key"))


@pytest.fixture
def api(tmp_path, authority):
    _skip_without_catalog()
    cfg = tmp_path / "service.yaml"
    cfg.write_text(json.dumps({
        "default_operator": "teller1",
        "session_signon": SIGNON,
        "capabilities": {
            SIGNON: {"allow_unapproved": True,
                     "allowed_principal_roles": ["supervisor", "teller"]},
            "meridian.member_lookup": {
                "allow_unapproved": True,
                "allowed_principal_roles": ["supervisor", "teller"]},
        }}))
    app = create_api(catalog_dir=CATALOG, service_config_path=str(cfg),
                     evidence_dir=str(tmp_path / "ev"), credential_store=CREDS,
                     session_authority=authority, require_session=None)
    return app.test_client()


def _as(authority, username):
    return {"X-Bankcua-Session": authority.mint(authority.store.get(username))}


# ---------------------------------------------------------------------------
# The capability leaves the model's action space
# ---------------------------------------------------------------------------
def test_a_signed_in_caller_is_never_offered_the_sign_on_capability(api,
                                                                   authority):
    """The manifest IS a chatbot's entire action space.

    Leaving sign-on in it would publish a tool that can only be called by
    mistake -- the session it would establish is the one the caller is already
    holding -- and the model has no way to know that from the contract.
    """
    for who in ("teller1", "super1"):
        names = [t["name"] for t in api.get("/capabilities",
                                            headers=_as(authority, who))
                 .get_json()]
        assert SIGNON not in names, f"{who} was offered {SIGNON}"
        assert names, "narrowing removed everything, which is not the point"


def test_the_direct_agent_path_still_sees_it(api):
    """Withholding it is a consequence of being signed in, not a deletion.

    Nobody has signed in on the direct agent path, so nothing has established a
    session and the capability is an ordinary one. Removing it from the catalog
    outright would have broken that caller to tidy up this one.
    """
    names = [t["name"] for t in api.get("/capabilities").get_json()]
    assert SIGNON in names


def test_the_session_endpoint_lists_what_the_console_may_actually_call(
        api, authority):
    body = api.get("/session", headers=_as(authority, "teller1")).get_json()
    assert SIGNON not in body["capabilities"]
    assert body["runs_as"] == "teller1"


def test_invoking_it_with_a_session_is_refused_before_a_browser_opens(
        api, authority):
    """The manifest narrowing is defence in depth; THIS is the defence.

    A caller that never read the manifest -- or read an older one -- gets the
    same answer, and gets it as a refusal in the ordinary contract rather than
    as an error, because nothing broke.
    """
    r = api.post(f"/invoke/{SIGNON}", headers=_as(authority, "teller1"),
                 json={"params": {}})
    assert r.status_code == 403
    body = r.get_json()
    assert body["status"] == "refused"
    assert body["refusal"]["code"] == "SIGNON_ESTABLISHED_AT_SIGN_IN"
    # A refusal has to say what would make it proceed. Here nothing the caller
    # can send would, and the requirement says so rather than implying a retry.
    assert "sign-in" in body["refusal"]["requirement"]


def test_the_assistant_says_it_is_already_done_rather_than_not_permitted(api):
    """A withheld capability and an already-done one need different sentences.

    "Not available under your sign-in" is true of a hold a teller asked for and
    actively misleading about a sign-on: it is missing BECAUSE they signed in.
    """
    manifest = [t for t in api.get("/capabilities").get_json()
                if t["name"] != SIGNON]
    reply = RuleRouter().route("sign on please", manifest)
    assert reply.capability_id is None
    assert "already signed on" in reply.unmatched_reason.lower()


# ---------------------------------------------------------------------------
# The one path that does run it
# ---------------------------------------------------------------------------
def test_establishing_a_sign_on_requires_a_signed_in_person(api):
    r = api.post("/session/signon")
    assert r.status_code == 401
    assert r.get_json()["refusal"]["code"] == "SESSION_REQUIRED"


def test_a_member_entry_cannot_reach_the_sign_on_door(api, authority):
    """A member has no Meridian operator identity, and cannot acquire one here.

    The refusal comes from the STORE rather than from a check on this route:
    the principal fixture still contains member 100234, and it cannot be
    resolved into a session at all, so there is no member-shaped caller for
    `/session/signon` to have to turn away.
    """
    from bankcua.auth import AuthError

    with pytest.raises(AuthError):
        authority.store.get("100234")


def test_a_deployment_that_names_no_sign_on_capability_has_no_such_door(
        tmp_path, authority):
    _skip_without_catalog()
    cfg = tmp_path / "service.yaml"
    cfg.write_text(json.dumps({"default_operator": "teller1"}))
    api = create_api(catalog_dir=CATALOG, service_config_path=str(cfg),
                     evidence_dir=str(tmp_path / "ev"), credential_store=CREDS,
                     session_authority=authority).test_client()
    assert api.post("/session/signon",
                    headers=_as(authority, "teller1")).status_code == 404
    # ...and the capability is an ordinary one again, for everyone.
    names = [t["name"] for t in api.get("/capabilities",
                                        headers=_as(authority, "teller1"))
             .get_json()]
    assert SIGNON in names


def test_an_unapproved_sign_on_capability_is_refused_rather_than_excused(
        tmp_path, authority):
    """The console does not get an exception from the approval gate.

    `allow_unapproved` is left at its closed default here, so a draft sign-on is
    refused at the door exactly as it would be at /invoke. What the console then
    does with that refusal -- let the person in unverified -- is the console's
    decision, made in the open, rather than the API quietly making one for it.
    """
    # A DRAFT copy, so the assertion is about the gate rather than about
    # whichever review state this repo's catalog happens to be in -- and so a
    # test can never be the thing that starts a browser against a live host.
    catalog = tmp_path / "capabilities"
    catalog.mkdir()
    art = json.loads(open(os.path.join(CATALOG, f"{SIGNON}.json")).read())
    art["approval_state"] = "draft"
    (catalog / f"{SIGNON}.json").write_text(json.dumps(art))

    cfg = tmp_path / "service.yaml"
    cfg.write_text(json.dumps({"default_operator": "teller1",
                               "session_signon": SIGNON}))
    api = create_api(catalog_dir=str(catalog), service_config_path=str(cfg),
                     evidence_dir=str(tmp_path / "ev"), credential_store=CREDS,
                     session_authority=authority).test_client()
    r = api.post("/session/signon", headers=_as(authority, "teller1"))
    assert r.status_code == 409
    assert r.get_json()["refusal"]["code"] == "CAPABILITY_NOT_APPROVED"


# ---------------------------------------------------------------------------
# The console's side of it
# ---------------------------------------------------------------------------
def _portal(tmp_path, principals, authority, verdict):
    return create_portal(
        catalog_dir=CATALOG, evidence_dirs=(str(tmp_path / "ev"),),
        principals_path=principals,
        session_key_path=str(tmp_path / "session.key"),
        session_authority=authority,
        signon_client=lambda token: verdict).test_client()


SIGNED_ON = ({"status": "success", "run_id": "meridian.signon-20260820"}, 200)
HOST_REFUSED = ({"status": "business_outcome", "run_id": "meridian.signon-x",
                 "business_outcome": {"code": "BAD_CREDENTIAL",
                                      "message": "Invalid operator ID."}}, 200)
API_DOWN = ({"error": "capability API unreachable at http://127.0.0.1:8080"},
            503)


def test_the_login_page_offers_the_operators_the_store_configures(
        tmp_path, principals, authority):
    """Read from the principal store, not written into the page.

    Adding an operator should be an edit to a config file. Tellers sort first
    because a list's default selection should be the least privileged identity
    that can do the job.
    """
    client = _portal(tmp_path, principals, authority, SIGNED_ON)
    page = client.get("/login").get_data(as_text=True)
    assert page.index('value="teller1"') < page.index('value="super1"')
    # ...and the member entry in the store is not on it: the list is built from
    # principals the store will actually resolve, and it refuses that one.
    assert 'value="100234"' not in page
    assert "Member number" not in page


def test_picking_an_operator_signs_in_and_signs_on(tmp_path, principals,
                                                   authority):
    client = _portal(tmp_path, principals, authority, SIGNED_ON)
    r = client.post("/login", data={"username": "teller1"})
    assert r.status_code == 302
    me = client.get("/me").get_json()
    assert me["signed_in"] and me["username"] == "teller1"
    assert me["signon"]["state"] == "signed_on"
    # The verdict points at the run that produced it rather than restating it:
    # the evidence the engine wrote stays the only account of what happened.
    assert me["signon"]["run_id"] == "meridian.signon-20260820"


def test_a_host_rejection_stops_the_sign_in(tmp_path, principals, authority):
    """The HOST answered about this credential, and the answer was no.

    Letting someone into a console whose every capability begins by signing on
    with that credential would only move the failure to their first request,
    with less to read.
    """
    client = _portal(tmp_path, principals, authority, HOST_REFUSED)
    r = client.post("/login", data={"username": "super1"})
    assert r.status_code == 401
    assert "Invalid operator ID." in r.get_data(as_text=True)
    assert client.get("/me").status_code == 401


def test_an_unreachable_target_signs_you_in_unverified_rather_than_locking_you_out(
        tmp_path, principals, authority):
    """A target that cannot be reached is not evidence about a credential.

    Failing closed here would mean an offline host locks every operator out of a
    console whose authorisation does not depend on that host at all -- so the
    console says what it does not know, in the header, and opens.
    """
    client = _portal(tmp_path, principals, authority, API_DOWN)
    assert client.post("/login", data={"username": "teller1"}).status_code == 302
    badge = client.get("/me").get_json()["signon"]
    assert badge["state"] == "unverified"
    assert "unreachable" in badge["detail"]
    assert "MERIDIAN not verified" in client.get("/").get_data(as_text=True)


def test_a_member_cannot_sign_in_with_or_without_a_password(
        tmp_path, principals, authority):
    """Both doors, because they are different code paths.

    A blank password takes the passwordless operator affordance; a supplied one
    takes `authenticate`. Both end at `PrincipalStore.get`, which is why one
    refusal covers both.
    """
    client = _portal(tmp_path, principals, authority, SIGNED_ON)
    assert client.post("/login",
                       data={"username": "100234"}).status_code == 401
    assert client.post("/login", data={"username": "100234",
                                       "password": "member123"}
                       ).status_code == 401
    assert client.get("/me").status_code == 401


def test_a_staff_password_is_still_verified_when_one_is_supplied(
        tmp_path, principals, authority):
    """The passwordless list is an affordance, not a removal of the check.

    A deployment that hardens this by putting the field back gets the
    verification for free, so the wrong password must not be waved through
    today.
    """
    client = _portal(tmp_path, principals, authority, SIGNED_ON)
    assert client.post("/login", data={"username": "teller1",
                                       "password": "wrong"}).status_code == 401
    assert client.post("/login", data={"username": "teller1",
                                       "password": "password"}
                       ).status_code == 302


def test_signing_out_drops_the_sign_on_verdict(tmp_path, principals, authority):
    """The verdict described a session that no longer exists.

    Keeping it would let the next sign-in inherit the last one's green badge
    without anything having signed on.
    """
    client = _portal(tmp_path, principals, authority, SIGNED_ON)
    client.post("/login", data={"username": "teller1"})
    assert client.get("/me").get_json()["signon"]["state"] == "signed_on"
    client.post("/logout")
    stale = _portal(tmp_path, principals, authority, API_DOWN)
    stale.post("/login", data={"username": "teller1"})
    assert stale.get("/me").get_json()["signon"]["state"] == "unverified"
