"""
CLI entrypoints (no browser needed): catalog list/show/manifest/approve,
codegen, the operator inbox listing, and the unattended approval-gate refusal.
"""
import ast
import json
import os
import shutil

import pytest

from bankcua.cli import main
from bankcua.schema import CapabilityArtifact

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
CAPS = os.path.join(ROOT, "capabilities")
ART = os.path.join(CAPS, "corebank.member_savings_lookup.json")


def _skip_if_missing():
    if not os.path.exists(ART):
        pytest.skip("artifacts missing; run scripts/run_discovery.sh")


def test_catalog_list(capsys):
    _skip_if_missing()
    main(["catalog", "list", "--dir", CAPS])
    out = capsys.readouterr().out
    assert "corebank.member_savings_lookup" in out
    assert "corebank.open_subaccount" in out


def test_catalog_manifest_is_agent_callable(capsys):
    _skip_if_missing()
    main(["catalog", "manifest", "--dir", CAPS])
    out = capsys.readouterr().out
    assert '"input_schema"' in out and '"member_id"' in out and '"returns"' in out


def test_catalog_show(capsys):
    _skip_if_missing()
    main(["catalog", "show", "--id", "corebank.member_savings_lookup", "--dir", CAPS])
    out = capsys.readouterr().out
    assert '"capability' not in out          # it's the artifact json, not an error
    assert "member_savings_lookup" in out


def test_catalog_approve_changes_state(tmp_path):
    _skip_if_missing()
    d = tmp_path / "caps"
    shutil.copytree(CAPS, d)
    main(["catalog", "approve", "--id", "corebank.member_savings_lookup",
          "--dir", str(d)])
    art = CapabilityArtifact.from_json(
        open(d / "corebank.member_savings_lookup.json").read())
    assert art.approval_state.value == "approved"


def test_codegen_writes_valid_python(tmp_path):
    _skip_if_missing()
    out = tmp_path / "gen.py"
    main(["codegen", "--artifact", ART, "--out", str(out)])
    ast.parse(out.read_text())
    assert "def run(" in out.read_text()


def test_operator_list_empty(tmp_path, capsys):
    main(["operator", "list", "--handoffs", str(tmp_path / "empty")])
    # no open interventions -> runs cleanly, prints nothing fatal
    assert "Error" not in capsys.readouterr().out


def test_replay_refused_when_unapproved(capsys):
    _skip_if_missing()
    # committed artifacts are draft; --require-approved must refuse (exit 4)
    with pytest.raises(SystemExit) as e:
        main(["replay", "--artifact", ART, "--require-approved",
              "--param", "username=operator", "--param", "password=password123",
              "--param", "member_id=12345"])
    assert e.value.code == 4
    assert "REFUSED" in capsys.readouterr().out


def test_replay_cli_end_to_end(mock_app, tmp_path, capsys):
    """Full replay CLI path: a --tenant override re-points the artifact at the
    test mock (dynamic port) so the whole command runs against a live surface."""
    pytest.importorskip("playwright")
    _skip_if_missing()
    tenant = tmp_path / "t.json"
    tenant.write_text(json.dumps(
        {"tenant_id": "test", "base_url": mock_app, "label_map": {}}))
    main(["replay", "--artifact", ART, "--tenant", str(tenant),
          "--evidence", str(tmp_path / "ev"),
          "--policy", os.path.join(ROOT, "config", "policy.yaml"),
          "--handoffs", str(tmp_path / "ho"),
          "--param", "username=operator", "--param", "password=password123",
          "--param", "member_id=12345"])
    out = capsys.readouterr().out
    assert '"status": "success"' in out
    assert '"savings_balance": 421355' in out


CREDS = ["--param", "username=operator", "--param", "password=password123",
         "--param", "member_id=12345"]


def _artifact_pointing_at(mock_app, dest):
    """Copy the committed artifact and rebind it to the test's mock instance."""
    art = CapabilityArtifact.from_json(open(ART).read())
    art.target.base_url = mock_app
    art.target.allowed_url_patterns = [f"{mock_app}/*", mock_app]
    dest.write_text(art.to_json())
    return art


def test_stability_write_back_refuses_a_tenant_bound_run(mock_app, tmp_path, capsys):
    """A tenant-bound run must never write back to the shared artifact.

    `--tenant` rebases the in-memory artifact: new base_url, new allowlist,
    locator strings remapped to that tenant's wording. Persisting that object to
    the path it was loaded from silently converts the shared, multi-tenant
    capability into a single-tenant one -- and the next reader has no way to tell,
    because the file still validates and still replays cleanly against Summit.

    Refusing is also the honest answer on the merits: a pass rate measured on one
    tenant is not evidence about the capability.
    """
    _skip_if_missing()
    art_path = tmp_path / "cap.json"
    _artifact_pointing_at(mock_app, art_path)
    before = art_path.read_text()

    tenant = tmp_path / "summit.json"
    tenant.write_text(json.dumps({
        "tenant_id": "summit-cu", "base_url": mock_app,
        "label_map": {"Member ID": "Member Number", "Search": "Find"}}))

    main(["replay", "--artifact", str(art_path), "--tenant", str(tenant),
          "--repeat", "2", "--update-stability",
          "--evidence", str(tmp_path / "ev"), *CREDS])

    out = capsys.readouterr().out
    assert "NOT writing stability" in out
    assert "summit-cu" in out
    assert art_path.read_text() == before, "the shared artifact was modified"


def test_stability_write_back_records_only_the_signal(mock_app, tmp_path):
    """Without a tenant override the write-back is allowed -- but it must carry
    ONLY the stability signal, re-read from disk, never whatever the run left on
    the in-memory object."""
    _skip_if_missing()
    art_path = tmp_path / "cap.json"
    original = _artifact_pointing_at(mock_app, art_path)

    main(["replay", "--artifact", str(art_path), "--repeat", "2",
          "--update-stability", "--evidence", str(tmp_path / "ev"), *CREDS])

    after = CapabilityArtifact.from_json(art_path.read_text())
    assert after.stability is not None and after.stability.runs == 2
    # everything else is byte-identical to what was there before
    a, b = after.model_dump(), original.model_dump()
    a.pop("stability"), b.pop("stability")
    assert a == b
