"""Agent-facing API: manifest, contract, and the approval gate on invoke."""
import os

import pytest

pytest.importorskip("playwright")

from bankcua.service import create_app
from bankcua.schema import CapabilityArtifact

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
ART = os.path.join(ROOT, "capabilities", "corebank.member_savings_lookup.json")


@pytest.fixture
def client():
    return create_app().test_client()


def test_manifest_lists_capabilities(client):
    if not os.path.exists(ART):
        pytest.skip("artifact missing")
    caps = client.get("/capabilities").get_json()
    names = [c["name"] for c in caps]
    assert "corebank.member_savings_lookup" in names
    tool = next(c for c in caps if c["name"] == "corebank.member_savings_lookup")
    assert "member_id" in tool["input_schema"]["properties"]
    assert "savings_balance" in tool["returns"]


def test_unknown_capability_404(client):
    assert client.post("/invoke/nope", json={"params": {}}).status_code == 404


def test_invoke_refused_when_unapproved(mock_app, client):
    if not os.path.exists(ART):
        pytest.skip("artifact missing")
    # ensure draft
    art = CapabilityArtifact.from_json(open(ART).read())
    if art.approval_state.value != "draft":
        pytest.skip("artifact already approved in this working copy")
    r = client.post("/invoke/corebank.member_savings_lookup",
                    json={"params": {"username": "operator", "password": "password123",
                                     "member_id": "12345"}})
    assert r.status_code == 409
    assert r.get_json()["error"] == "capability not approved"


def test_invoke_runs_with_allow_unapproved(mock_app):
    if not os.path.exists(ART):
        pytest.skip("artifact missing")
    # point the service at the fixture's live mock (dynamic port)
    app = create_app(base_url_override=mock_app)
    r = app.test_client().post(
        "/invoke/corebank.member_savings_lookup",
        json={"params": {"username": "operator", "password": "password123",
                         "member_id": "12345"}, "allow_unapproved": True})
    assert r.status_code == 200
    d = r.get_json()
    assert d["status"] == "success"
    assert d["outputs"]["savings_balance"] == 421355
