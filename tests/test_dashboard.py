"""
The dashboard, and the property that makes it trustworthy.

It has no store. Everything it shows is derived from the evidence the replay
engine already wrote. Two things follow, and both are asserted here rather than
described: it must never write, because a second account of what happened would
eventually disagree with the first; and it must present the five-way result
contract as five distinct things, because collapsing them into a tick and a cross
in the UI would undo the distinction at the last possible step.

The evidence route is also the one place in this system that takes a filesystem
path from a URL, on a host that holds screenshots of member accounts -- so the
traversal check gets a test of its own.
"""
import ast
import json
import os

import pytest

from bankcua.dashboard import STATUS_STYLES, create_app, evidence_counts

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")
DASHBOARD = os.path.join(ROOT, "bankcua", "dashboard.py")
CATALOG = os.path.join(ROOT, "capabilities", "meridian")


@pytest.fixture
def evidence(tmp_path):
    """A fabricated evidence tree: the dashboard's whole input is files on disk,
    so it can be exercised without a browser or a target."""
    run = tmp_path / "meridian.member_lookup-20260820-000000-000"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({
        "status": "business_outcome", "capability_id": "meridian.member_lookup",
        "version": "1.0.1", "outputs": {"shares": [{"Share ID": "1", "Balance": "$1"}]},
        "business_outcome": {"code": "NO_SEARCH_MATCHES", "message": "none"},
        "steps_executed": 8, "duration_s": 1.2, "recoveries": [], "drifts": []}))
    (run / "run.jsonl").write_text(
        json.dumps({"ts": 1, "event": "replay_started"}) + "\n"
        + json.dumps({"ts": 2, "event": "condition_detected",
                      "code": "NO_SEARCH_MATCHES"}) + "\n")
    (run / "step00.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return tmp_path, run.name


@pytest.fixture
def client(evidence):
    root, _ = evidence
    return create_app(catalog_dir=CATALOG, evidence_dirs=(str(root),)).test_client()


def test_the_dashboard_never_writes():
    """Asserted from the source, because a stray write is invisible in a demo.

    If this module ever opens a file for writing, or imports a storage layer, it
    has stopped being a projection and started being a second source of truth --
    and the one people trust will be whichever is easier to edit.
    """
    tree = ast.parse(open(DASHBOARD).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = node.args[1].value
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            assert "w" not in mode and "a" not in mode, (
                f"dashboard opens a file for writing (mode={mode!r})")


def test_status_vocabulary_covers_the_whole_result_contract():
    """The UI's vocabulary must not drift from ReplayStatus. A status with no
    style renders as an unlabelled grey pill -- which is how a FAILURE ends up
    looking like a business outcome on a wallboard."""
    from bankcua.replay.result import ReplayStatus
    assert {s.value for s in ReplayStatus} == set(STATUS_STYLES)
    assert len({css for _label, css in STATUS_STYLES.values()}) == len(STATUS_STYLES)


def test_catalog_view_surfaces_the_approval_gate(client):
    caps = client.get("/api/capabilities").get_json()
    if not caps:
        pytest.skip("meridian capabilities not recorded")
    by_id = {c["id"]: c for c in caps}
    hold = by_id.get("meridian.place_hold")
    assert hold is not None
    # A reviewer must be able to see, without opening a file, that a capability
    # has irreversible steps and whether anyone has ratified them.
    assert hold["risky_steps"], "place_hold should carry an irreversible step"
    assert "unreviewed_risky" in hold and "approval_state" in hold


def test_run_index_reports_the_outcome_code_not_just_the_status(client, evidence):
    _root, run_id = evidence
    runs = client.get("/api/runs").get_json()["shown"]
    row = next(r for r in runs if r["run_id"] == run_id)
    assert row["status"] == "business_outcome"
    assert row["code"] == "NO_SEARCH_MATCHES", (
        "the code is the actionable half; a bare status says only that it was "
        "not the happy path")


def test_run_detail_includes_the_redacted_event_log(client, evidence):
    _root, run_id = evidence
    detail = client.get(f"/api/runs/{run_id}").get_json()
    assert detail["summary"]["capability_id"] == "meridian.member_lookup"
    assert [e["event"] for e in detail["events"]] == ["replay_started",
                                                      "condition_detected"]
    assert "step00.png" in detail["evidence"]


def test_evidence_is_served_only_for_files_the_run_produced(client, evidence):
    _root, run_id = evidence
    assert client.get(f"/api/runs/{run_id}/evidence/step00.png").status_code == 200
    # The path comes from a URL, on a host that also holds screenshots of member
    # accounts. Without the membership check this walks the filesystem.
    for probe in ("../../../etc/passwd", "..%2f..%2fconfig/credentials.json",
                  "summary.json"):
        assert client.get(
            f"/api/runs/{run_id}/evidence/{probe}").status_code == 404


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/nope").status_code == 404


def test_an_intervention_id_cannot_climb_out_of_the_handoff_store(tmp_path):
    """The same defect as the evidence route, on the routes next to it.

    Intervention ids are matched with `<path:...>`, and `HandoffStore` turns one
    straight into `<root>/<id>.json`. `..%2fsecret` therefore loaded a JSON file
    from anywhere on the host and handed whatever `screenshot_path` it named to
    `send_file` -- on a host that holds screenshots of member accounts. The
    evidence route refuses this by checking membership in the run's own file
    list; these routes have no such list, so they check the shape instead.
    """
    handoffs = tmp_path / "handoffs"
    handoffs.mkdir()
    # A JSON file OUTSIDE the store that a traversal would otherwise reach.
    (tmp_path / "secret.json").write_text(json.dumps({
        "id": "secret", "kind": "dual_control", "reason": "should be unreachable",
        "screenshot_path": "/etc/hosts"}))

    client = create_app(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),),
                        handoff_dir=str(handoffs)).test_client()
    # follow_redirects, because Werkzeug normalises a decoded leading slash into
    # a 308 before routing ever sees it -- and a probe that stops at the redirect
    # would pass without the guard being exercised at all.
    for probe in ("..%2fsecret", "../secret", "..%2f..%2fsecret",
                  "%2fetc%2fhosts"):
        started = client.post(f"/api/interventions/{probe}/console",
                              follow_redirects=True)
        assert started.status_code == 404, (probe, started.get_data(as_text=True))
        shot = client.get(f"/api/interventions/{probe}/screenshot",
                          follow_redirects=True)
        assert shot.status_code == 404, (probe, shot.status_code)


def test_evidence_counts_summarise_a_tree(evidence):
    root, _ = evidence
    assert evidence_counts(str(root)) == {"business_outcome": 1}


def test_the_dashboard_invokes_through_the_api_not_the_engine():
    """It is the demo driver, so it must drive the REAL path.

    Calling the replay engine directly would be faster and would look identical
    on screen -- and it would bypass every server-side authorisation check in
    config/service.yaml, so a reviewer watching the dashboard would be watching a
    privileged path that no real caller has. Asserted structurally, because the
    difference is invisible in a demo.
    """
    tree = ast.parse(open(DASHBOARD).read())
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            reached.add(node.module)
        elif isinstance(node, ast.Import):
            reached.update(a.name for a in node.names)
    assert "urllib.request" in reached, "the dashboard must call the API over HTTP"
    forbidden = {".replay.engine", "replay.engine", ".safety.credentials",
                 ".surface.web_playwright"}
    assert not (reached & forbidden), (
        f"dashboard reaches past the API into {reached & forbidden}")


def test_the_invoke_proxy_preserves_the_result_contract(tmp_path, monkeypatch):
    """A refusal is a 403 and a business outcome is a 200. Both are ANSWERS, so
    the proxy must pass the body and the status through rather than collapsing a
    non-2xx into an error -- which is the distinction the whole system exists to
    preserve, at the one screen a reviewer actually looks at."""
    import bankcua.dashboard as dash

    captured = {}

    def fake_call(base_url, path, body=None, token=""):
        captured["path"] = path
        captured["body"] = body
        captured["token"] = token
        return ({"status": "refused", "capability_id": "x",
                 "refusal": {"code": "ROLE_NOT_PERMITTED",
                             "requirement": "a supervisor"}}, 403)

    monkeypatch.setattr(dash, "_call_api", fake_call)
    app = dash.create_app(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),))
    r = app.test_client().post("/api/invoke/meridian.place_hold",
                               json={"operator": "teller1",
                                     "params": {"member_id": "100234"}})
    assert r.status_code == 403
    assert r.get_json()["refusal"]["code"] == "ROLE_NOT_PERMITTED"
    assert captured["path"] == "/invoke/meridian.place_hold"
    assert captured["body"]["operator"] == "teller1"


def test_the_dashboard_never_forwards_a_credential(tmp_path, monkeypatch):
    """It names an operator alias. If it ever forwarded a password field, the
    credential store would be decorative."""
    import bankcua.dashboard as dash

    captured = {}

    def fake_call(base_url, path, body=None, token=""):
        captured["body"] = body
        return ({"status": "success", "outputs": {}}, 200)

    monkeypatch.setattr(dash, "_call_api", fake_call)
    app = dash.create_app(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),))
    app.test_client().post("/api/invoke/meridian.member_lookup",
                           json={"operator": "teller1",
                                 "params": {"member_id": "100234",
                                            "password": "attacker-supplied"}})
    # The proxy forwards params verbatim; the SERVICE overwrites secrets after
    # the caller's params, so a smuggled field cannot win. What must never happen
    # is the dashboard adding one of its own.
    assert "password" not in json.dumps(captured["body"]).replace(
        '"password": "attacker-supplied"', "")


def test_run_history_is_capped_but_reports_the_true_total(tmp_path):
    """A truncated list must never look like the whole history.

    Evidence accumulates: a working session leaves dozens of runs behind, and a
    reviewer who sees a capped list with no total can conclude that a scenario
    never ran. The cap keeps the page usable; the total keeps it honest.
    """
    for i in range(7):
        run = tmp_path / f"meridian.member_lookup-2026082{i}-000000-000"
        run.mkdir()
        (run / "summary.json").write_text(json.dumps(
            {"status": "success", "capability_id": "meridian.member_lookup",
             "steps_executed": 1, "duration_s": 0.1, "outputs": {}}))
    app = create_app(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),))
    body = app.test_client().get("/api/runs?limit=3").get_json()
    assert len(body["shown"]) == 3
    assert body["total"] == 7


def test_a_nonsense_limit_does_not_break_the_page(tmp_path):
    """The limit arrives from a URL. A page that 500s on `?limit=banana` is a
    page that fails in front of whoever is being shown the demo."""
    app = create_app(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),))
    c = app.test_client()
    for probe in ("banana", "-5", "999999999"):
        assert c.get(f"/api/runs?limit={probe}").status_code == 200


def _write_intervention(root, req_id, created_at=1000.0):
    """A minimal open, attachable intervention on disk.

    The console endpoint reads the record before doing anything, because the
    registry is keyed on (id, created_at) -- ids repeat across escalations.
    """
    from bankcua.escalation.handoff import (HandoffStore, InterventionKind,
                                            InterventionRequest)
    HandoffStore(str(root)).write(InterventionRequest(
        id=req_id, kind=InterventionKind.RISKY_CONFIRMATION, reason="test",
        cdp_endpoint="http://127.0.0.1:9222", created_at=created_at))


def test_the_dashboard_can_start_the_console_itself(tmp_path, monkeypatch):
    """Taking control must not require dropping to a terminal.

    The console is a Flask app over the existing CDP seam, so the dashboard can
    start it on request. That changes nothing about the control-transfer model --
    same live session, same recorded human actions, same control token handed
    back -- and everything about whether anyone watching the demo ever sees it.
    """
    import bankcua.dashboard as dash

    started = {}

    class _FakeApp:
        def __init__(self):
            self.config = {"bankcua_worker": type("W", (), {"_closed": False})()}

        def run(self, **kw):
            started["port"] = kw.get("port")

    monkeypatch.setattr(dash, "create_console", lambda rid, handoffs: _FakeApp())
    _write_intervention(tmp_path, "some-intervention")
    app = dash.create_app(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),),
                          handoff_dir=str(tmp_path), console_port=8099)
    c = app.test_client()
    body = c.post("/api/interventions/some-intervention/console").get_json()
    assert body["url"].endswith(":8099") and body["started"] is True


def test_starting_a_console_twice_reuses_the_one_already_running(tmp_path,
                                                                 monkeypatch):
    """One console per intervention. Two of them driving the same paused banking
    page is exactly the interleaving the single-owner session model exists to
    prevent -- and the second would fail to bind the port anyway."""
    import bankcua.dashboard as dash

    class _FakeApp:
        def __init__(self):
            self.config = {"bankcua_worker": type("W", (), {"_closed": False})()}

        def run(self, **kw):
            import time
            time.sleep(5)          # stays "alive" for the duration of the test

    monkeypatch.setattr(dash, "create_console", lambda rid, handoffs: _FakeApp())
    _write_intervention(tmp_path, "x")
    app = dash.create_app(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),),
                          handoff_dir=str(tmp_path), console_port=8098)
    c = app.test_client()
    assert c.post("/api/interventions/x/console").get_json()["started"] is True
    assert c.post("/api/interventions/x/console").get_json()["started"] is False


def test_an_unattachable_intervention_reports_why_rather_than_crashing(tmp_path,
                                                                      monkeypatch):
    """A run started without a CDP port exposes no session to take control of.
    An operator meeting a stack trace while a bank screen waits on them is the
    worst possible moment to debug."""
    import bankcua.dashboard as dash
    from bankcua.escalation.console import NotAttachable

    def _raise(rid, handoffs):
        raise NotAttachable("the run was started without --cdp-port")

    monkeypatch.setattr(dash, "create_console", _raise)
    _write_intervention(tmp_path, "x")
    app = dash.create_app(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),),
                          handoff_dir=str(tmp_path))
    r = app.test_client().post("/api/interventions/x/console")
    assert r.status_code == 409
    assert "cdp-port" in r.get_json()["error"]


def test_the_escalation_panel_does_not_rebuild_itself_on_every_poll():
    """Polling must never destroy live UI.

    The panel refreshes every 2.5s so an escalation is noticed while it is still
    open. The first version rebuilt its HTML unconditionally, which tore out the
    console iframe an operator was working in and put the still screenshot back
    -- the panel visibly flickered between the two while somebody was mid-handoff
    on a banking screen.

    Asserted from the source because it is a timing behaviour: it looks correct
    in a screenshot and only shows up while watching.
    """
    source = open(DASHBOARD).read()
    assert "ESC_SIG" in source, "the panel has no render guard"
    assert "if(sig === ESC_SIG) return;" in source, (
        "the panel rebuilds unconditionally and will destroy a live console")
    assert "CONSOLES[ckey]" in source, (
        "a legitimate re-render must restore the live console, not revert to "
        "the still screenshot")


def test_a_second_escalation_never_inherits_a_spent_console(tmp_path, monkeypatch):
    """Intervention ids repeat, so identity needs more than the id.

    Every `place_hold` pause is called `replay-meridian.place_hold-step10` --
    the id is derived from capability and step. Keying the console registry on
    the id alone handed the SECOND escalation the FIRST one's console, which had
    already handed control back and serves nothing but its "closed" page. The
    operator presses Take control and is told control has been returned: from
    their side, indistinguishable from a broken handoff.
    """
    import bankcua.dashboard as dash
    from bankcua.escalation.handoff import (HandoffStore, InterventionKind,
                                            InterventionRequest)

    store = HandoffStore(str(tmp_path / "handoffs"))
    started = []

    class _FakeWorker:
        def __init__(self): self._closed = False

    class _FakeApp:
        def __init__(self): self.config = {"bankcua_worker": _FakeWorker()}
        def run(self, **kw):
            started.append(kw.get("port"))
            import time
            time.sleep(10)

    monkeypatch.setattr(dash, "create_console", lambda rid, handoffs: _FakeApp())
    app = dash.create_app(catalog_dir=CATALOG, evidence_dirs=(str(tmp_path),),
                          handoff_dir=str(tmp_path / "handoffs"),
                          console_port=8190)
    client = app.test_client()

    def escalate(created_at):
        req = InterventionRequest(id="replay-x-step10",
                                  kind=InterventionKind.RISKY_CONFIRMATION,
                                  reason="r", cdp_endpoint="http://127.0.0.1:9222",
                                  created_at=created_at)
        store.write(req)

    escalate(1000.0)
    first = client.post("/api/interventions/replay-x-step10/console").get_json()
    # same intervention, asked twice -> one console, not two on the same session
    again = client.post("/api/interventions/replay-x-step10/console").get_json()
    assert again["started"] is False and again["url"] == first["url"]

    # a NEW escalation reusing the id must get its own console
    escalate(2000.0)
    second = client.post("/api/interventions/replay-x-step10/console").get_json()
    assert second["started"] is True
    assert second["url"] != first["url"], (
        "the second escalation inherited the first one's console")
