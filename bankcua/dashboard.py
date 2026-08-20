"""
A window onto what the system did: catalog, run history, and per-run evidence.

Why this is read-only, and why that is the design and not a shortcut
--------------------------------------------------------------------
The dashboard has no store of its own. Every row it shows is derived by reading
the `summary.json` and `run.jsonl` that the replay engine already writes, plus
the screenshots and DOM snapshots it already captures. Giving it a database would
create a second account of what happened, and the two would eventually disagree
-- at which point the thing a reviewer trusts is the one that is easier to write
to, rather than the one the automation actually produced.

So: the engine's evidence is the single source of truth, and this is a
projection. It never writes, and `tests/test_dashboard.py` asserts that by
opening no file for writing anywhere in this module.

It is also the demo driver: it INVOKES capabilities through the same public
capability API any other caller uses, over HTTP, holding no credentials and
receiving no special treatment. That is what keeps it a window rather than a back
door -- server-side authorisation in `config/service.yaml` applies to it exactly
as it applies to the chatbot, so a capability the deployment has not approved
cannot be run from here either.

It also displays run STATUS using the same five-way contract the rest of the
system speaks -- success / business outcome / refused / escalated / failure --
because collapsing them into a green tick and a red cross in the UI would undo,
at the last step, the distinction the whole system exists to preserve. A reviewer
looking at this screen should be able to see at a glance that a "no such member"
and a crash are not the same event.

Who is looking at it
--------------------
The same window shows different things to different people, and that is an
authorisation decision rather than a presentation one -- so it is made on the
server and never in the page. A member of the public signs in and gets a console
that can describe exactly one member: their own record, their own runs, their own
evidence. Staff get the operator view.

The rule that makes this safe is that the projection is filtered BEFORE it is
serialised. A page that fetched every run and hid the other members' rows in CSS
would be one "view source" away from disclosing them, and every route below that
takes a run id re-checks ownership rather than trusting that the id came from a
list the caller was allowed to see.

Invocation is unchanged: it still goes through the capability API, which
independently re-resolves the same session token and applies the same rules. If
this page's filtering were removed entirely, a member still could not invoke
anything against another member's account.

The seam: swap this for any other viewer without touching how runs record
evidence.
"""
from __future__ import annotations

import glob
import json
import os
import threading
import time
import urllib.error
import urllib.request

from flask import Flask, abort, jsonify, request, send_file

from .auth import (AuthError, Principal, SessionAuthority, SESSION_HEADER,
                   token_from_request)
from .catalog import Catalog
from .escalation.console import NotAttachable, create_console
from .escalation.handoff import (HandoffStore, InterventionStatus)
from .service import _run_detail, _run_index

#: Status -> (label, css class). Held as data so the vocabulary cannot drift from
#: ReplayStatus by someone editing a template.
STATUS_STYLES = {
    "success": ("SUCCESS", "success"),
    "business_outcome": ("BUSINESS OUTCOME", "business"),
    "refused": ("REFUSED", "refused"),
    "escalated": ("ESCALATED", "escalated"),
    "failure": ("FAILURE", "failure"),
}


def _is_safe_id(ident: str) -> bool:
    """Is this an identifier, rather than a path?

    Intervention ids are matched with `<path:...>` because they are long and
    dotted, and `HandoffStore` turns one straight into `<root>/<id>.json`. That
    join is only safe while the id cannot climb out of the store: `..%2fsecret`
    resolves, loads a JSON file from anywhere on the host, and hands whatever
    `screenshot_path` it names to `send_file`. The evidence route below already
    refuses this by checking membership in the run's own file list; these routes
    have no such list to check against, so they check the shape instead.
    """
    return bool(ident) and ".." not in ident and "/" not in ident \
        and "\\" not in ident and not os.path.isabs(ident)


def _call_api(base_url: str, path: str, body: dict | None = None,
              token: str = "") -> tuple[dict, int]:
    """Talk to the capability API as an ordinary client.

    A refusal is a 403 and a business outcome is a 200: both are ANSWERS, so the
    body is what matters and a non-2xx must not be raised. Collapsing "the bank
    declined" into "the call failed" is the exact distinction this system exists
    to preserve, and the dashboard is where a reviewer looks to see it.
    """
    url = base_url.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        # The session travels as a header, so the API can re-derive the subject
        # itself. The dashboard is not trusted to report who is asking: it says
        # who signed in by forwarding what they were given, and the API decides
        # what that is worth.
        headers[SESSION_HEADER] = token
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.load(r), r.status
    except urllib.error.HTTPError as ex:
        try:
            return json.load(ex), ex.code
        except Exception:
            return {"error": ex.reason}, ex.code
    except Exception as ex:
        return {"error": f"capability API unreachable at {base_url}: {ex}"}, 503


def create_app(catalog_dir: str = "capabilities/meridian",
               evidence_dirs: tuple[str, ...] = ("evidence/service",
                                                 "evidence/meridian"),
               api_url: str = "http://127.0.0.1:8080",
               handoff_dir: str = "evidence/handoffs",
               console_port: int = 8090,
               session_authority: SessionAuthority | None = None,
               require_session: bool = False) -> Flask:
    app = Flask(__name__)
    # A grid's column order is part of its meaning -- "Share ID, Type,
    # Balance, Status" is how the operator reads the screen. Flask sorts
    # JSON keys by default, which silently alphabetises every extracted
    # table on the way out.
    app.json.sort_keys = False
    auth = session_authority or SessionAuthority()

    # ---- who is looking --------------------------------------------------
    def _principal() -> Principal | None:
        """The signed-in subject, or None when the console is running open.

        `require_session` is what the portal turns on. Left off, this is the
        single-operator dashboard the CLI has always started, which keeps the
        existing demo path working -- but then nobody is signed in, so nothing
        below narrows and the page is the operator view.
        """
        token = token_from_request(request.headers, request.cookies)
        if not token:
            return None
        try:
            return auth.principal(token)
        except AuthError:
            return None

    def _token() -> str:
        return token_from_request(request.headers, request.cookies)

    def _gate() -> Principal | None:
        """Resolve the subject or abort. Every data route calls this."""
        principal = _principal()
        if principal is None and require_session:
            abort(401)
        return principal

    def _owns(principal: Principal | None, record: dict) -> bool:
        """May this subject see this run?

        Staff see everything. A member sees runs recorded as theirs -- and a run
        with NO recorded subject (one made from the CLI, before anyone signed
        in) is not theirs. Failing closed there matters: those runs are exactly
        the operator-driven ones, over whichever member the operator was working
        on.
        """
        if principal is None or not principal.is_member:
            return True
        return str((record.get("principal") or {}).get("member_id") or "") == \
            str(principal.member_id)

    def _all_runs(principal: Principal | None = None) -> list[dict]:
        runs: list[dict] = []
        for root in evidence_dirs:
            for run in _run_index(root):
                if not _owns(principal, run):
                    continue
                run["evidence_root"] = root
                runs.append(run)
        return sorted(runs, key=lambda r: r["started_at"], reverse=True)

    def _find(run_id: str, principal: Principal | None = None):
        """Locate a run, refusing one the subject does not own.

        Ownership is re-checked HERE rather than assumed from the list the
        caller was shown. A run id is guessable -- it is a capability name and a
        timestamp -- and the routes below this one serve screenshots of member
        accounts.
        """
        for root in evidence_dirs:
            detail = _run_detail(root, run_id)
            if detail is not None:
                if not _owns(principal, detail):
                    return None, None
                return root, detail
        return None, None

    def _permitted_capabilities(principal: Principal | None) -> set[str] | None:
        """The capability names this subject may invoke, per the API.

        Asked of the API rather than decided here, because the API is where the
        answer is authoritative -- duplicating the rules in the viewer is how a
        console ends up offering a button that is always refused. None means "no
        narrowing" (nobody is signed in); an empty set means the API could not
        say, and for a member that is deliberately read as "show nothing".
        """
        if principal is None:
            return None
        body, status = _call_api(api_url, "/session", token=_token())
        if status != 200 or not isinstance(body, dict):
            return set()
        return set(body.get("capabilities") or [])

    @app.get("/")
    def index():
        """One console, two audiences.

        A member gets a different PAGE, not the operator page with parts hidden:
        the operator page is built around a catalog, a run history and an
        escalation queue, none of which are a member's business, and a version
        of it with those panels suppressed in the browser would still have
        fetched them.
        """
        principal = _gate()
        page = (_MEMBER_PAGE if principal is not None and principal.is_member
                else _INDEX_PAGE)
        return page.replace("__PREFIX__", request.script_root or "")

    @app.get("/api/me")
    def me():
        """Who the console thinks is signed in, for the page header."""
        principal = _gate()
        if principal is None:
            return jsonify({"signed_in": False})
        body, status = _call_api(api_url, "/session", token=_token())
        return jsonify({"signed_in": True, **principal.to_public(),
                        "runs_as": (body or {}).get("runs_as", ""),
                        "capabilities": sorted((body or {}).get("capabilities")
                                               or []) if status == 200 else []})

    @app.get("/api/me/details")
    def my_details():
        """A member's own record, as the evidence last recorded it.

        Derived from their most recent successful lookup rather than from a
        store of its own -- same rule as everything else on this page. It means
        a member sees exactly what the automation actually read off Meridian,
        and when it was read, rather than a cached profile that can quietly
        disagree with the bank.
        """
        principal = _gate()
        if principal is None or not principal.is_member:
            return jsonify({"error": "not a member session"}), 403
        for run in _all_runs(principal):
            if run["capability_id"] != "meridian.member_lookup":
                continue
            if run["status"] not in ("success", "business_outcome"):
                continue
            if run.get("outputs"):
                return jsonify({"member_id": principal.member_id,
                                "as_of_run": run["run_id"],
                                "as_of": run["started_at"],
                                "status": run["status"],
                                "outputs": run["outputs"]})
        return jsonify({"member_id": principal.member_id, "outputs": {},
                        "as_of_run": None})

    @app.get("/api/capabilities")
    def capabilities():
        principal = _gate()
        permitted = _permitted_capabilities(principal)
        cat = Catalog(catalog_dir)
        out = []
        for art in cat.list():
            if permitted is not None and art.id not in permitted:
                continue
            out.append({
                "id": art.id, "name": art.name, "version": art.version,
                "approval_state": art.approval_state.value,
                "description": art.description,
                "inputs": [{"name": i.name, "type": i.type.value,
                            "required": i.required, "sensitive": i.sensitive}
                           for i in art.inputs],
                "outputs": [{"name": o.name, "type": o.type.value,
                             "cardinality": o.cardinality.value}
                            for o in art.outputs],
                "steps": len(art.steps),
                "risky_steps": [s.index for s in art.steps
                                if s.risk.value == "risky"],
                "unreviewed_risky": Catalog.unreviewed_risky_steps(art),
                "conditions": len(art.known_conditions),
            })
        return jsonify(sorted(out, key=lambda c: c["id"]))

    @app.get("/api/operators")
    def operators():
        _gate()
        body, _status = _call_api(api_url, "/operators", token=_token())
        return jsonify(body)

    @app.get("/api/manifest")
    def manifest():
        """The contract as the API publishes it -- credentials and
        service-supplied identity already filtered out -- so the invoke form
        asks for exactly what a caller must decide and nothing else.

        Narrowed by the session as well: the API returns the manifest as this
        subject may see it, so a member's invoke form has no member number
        field to type somebody else's number into."""
        _gate()
        body, _status = _call_api(api_url, "/capabilities", token=_token())
        return jsonify(body)

    @app.post("/api/invoke/<cap_id>")
    def invoke(cap_id):
        """Proxy an invocation to the capability API.

        Deliberately a proxy and not a direct call into the engine: routing it
        through the public API means the dashboard is subject to the same
        server-side authorisation as any other caller, and a reviewer watching
        this screen is watching the real path rather than a privileged one.
        """
        principal = _gate()
        payload = request.get_json(force=True, silent=True) or {}
        # A signed-in caller does not choose an operator: the API derives the
        # alias from the session and refuses a request that names a different
        # one. Sending nothing is therefore both simpler and the only thing that
        # can be right.
        operator = None if principal is not None else payload.get("operator")
        body, status = _call_api(api_url, f"/invoke/{cap_id}", {
            **({"operator": operator} if operator else {}),
            "params": payload.get("params") or {},
            **({"approver": payload["approver"]} if payload.get("approver") else {}),
        }, token=_token())
        return jsonify(body), status

    #: Consoles started from this process. One per intervention: the console
    #: owns a single CDP connection to a single paused session, and two of them
    #: driving the same live banking page is the interleaving that ESC-07 exists
    #: to prevent.
    #:
    #: Keyed by (id, created_at), NOT by id alone. Intervention ids are derived
    #: from capability and step, so every `place_hold` escalation is called
    #: `replay-meridian.place_hold-step10`. Keying on the id alone meant the
    #: SECOND escalation was handed the FIRST one's console -- which had already
    #: handed control back and serves nothing but its "closed" page. From the
    #: operator's side that is indistinguishable from a broken handoff: they
    #: press Take control and are told control has been returned.
    consoles: dict[tuple, dict] = {}
    consoles_lock = threading.Lock()
    #: Flask's development server cannot be stopped once started, so a spent
    #: console keeps its port. The next one takes the next port rather than
    #: failing to bind.
    next_port = [console_port]

    @app.post("/api/interventions/<path:req_id>/console")
    def start_console(req_id):
        """Start the operator console for one intervention, in-process.

        Previously this was a terminal command a reviewer had to run at the most
        interesting moment of the demo. The console is just a Flask app over the
        existing CDP seam, so the dashboard can start it on request -- which
        changes nothing about the control-transfer model and everything about
        whether anyone sees it.

        It is NOT a shortcut past the handoff: the console still attaches to the
        same live session, still records every human action onto the
        intervention, and still hands the control token back on resolve.
        """
        principal = _gate()
        if principal is not None and principal.role != "supervisor":
            # Taking control means finishing an action on a live banking session
            # that replay refused to complete unattended. The whole point of the
            # pause is that a SUPERVISOR completes it; letting a teller -- let
            # alone a member -- attach would make the escalation ceremonial.
            return jsonify({"error": "taking control of a paused session is "
                                     "supervisor work"}), 403
        if not _is_safe_id(req_id):
            return jsonify({"error": "unknown intervention"}), 404
        try:
            request_record = HandoffStore(handoff_dir).read(req_id)
        except Exception:
            return jsonify({"error": "unknown intervention"}), 404
        if request_record.kind.value == "dual_control":
            # Checked before attaching, because the attach failure says "re-run
            # with --cdp-port", which is wrong AND actionable-looking: this
            # pause has no session by design and never will.
            return jsonify({"error": "this pause needs a counter-signature from "
                                     "a second supervisor, not control of a "
                                     "session -- nothing has been opened on the "
                                     "member's account yet"}), 409
        key = (req_id, request_record.created_at)

        with consoles_lock:
            existing = consoles.get(key)
            if existing and existing["thread"].is_alive() and not \
                    existing["worker"]._closed:
                return jsonify({"url": existing["url"], "started": False})
            try:
                console_app = create_console(req_id, handoffs=handoff_dir)
            except NotAttachable as ex:
                return jsonify({"error": str(ex)}), 409
            except Exception as ex:
                return jsonify({"error": f"could not open a console: {ex}"}), 500

            port = next_port[0]
            next_port[0] += 1
            url = f"http://127.0.0.1:{port}"
            thread = threading.Thread(
                target=lambda: console_app.run(host="127.0.0.1", port=port,
                                               threaded=True, use_reloader=False),
                daemon=True)
            thread.start()
            consoles[key] = {"thread": thread, "url": url,
                             "worker": console_app.config["bankcua_worker"]}
        return jsonify({"url": url, "started": True})

    @app.get("/api/interventions")
    def interventions():
        """Runs currently paused waiting for a person.

        Surfaced here because an escalation is invisible otherwise: the caller's
        request simply hangs, and nothing on screen says a human is being waited
        for. A dashboard that shows finished runs but not the one that STOPPED
        is showing the least interesting state.
        """
        principal = _gate()
        if principal is not None and principal.is_member:
            # A paused run is an operator queue. Its reason text and screenshot
            # describe whichever member the operator was working on, which is
            # categorically not something to show a member of the public.
            return jsonify([])
        out = []
        for req in HandoffStore(handoff_dir).list_open():
            # Two different things stop a run, and they need different
            # answers from a person. A gated irreversible step needs someone to
            # DRIVE the paused screen. A dual-control pause needs a second
            # person to APPROVE -- it happens before the browser has been sent
            # anywhere, so there is no screen to drive, and offering one put an
            # operator in front of a blank page whose only exit was aborting the
            # run.
            dual = req.kind.value == "dual_control"
            out.append({
                "id": req.id, "kind": req.kind.value, "reason": req.reason,
                "capability_id": req.capability_id,
                "needs": "countersignature" if dual else "control",
                "initiator": req.initiator,
                "step": req.current_step_index, "url": req.state_url,
                "cdp_endpoint": req.cdp_endpoint,
                "attachable": bool(req.cdp_endpoint) and not dual,
                # Part of the identity, because ids repeat across escalations:
                # every place_hold pause is called ...place_hold-step10.
                "created_at": req.created_at,
                "screenshot": os.path.basename(req.screenshot_path)
                if req.screenshot_path else None,
                "screenshot_path": req.screenshot_path,
                "console_url": f"http://127.0.0.1:{console_port}",
            })
        return jsonify(out)

    @app.post("/api/interventions/<path:req_id>/countersign")
    def countersign(req_id):
        """Record a second person's approval of a dual-control pause.

        The identity comes from the SESSION, never from the request body: an
        approver a caller can type is not a second pair of eyes, it is a string.
        Two checks live here because they are about the console's own subject --
        that a member cannot counter-sign anything, and that the person clicking
        is not the person who started the run. The engine re-checks the second
        one anyway against the policy's approver registry, because a resolution
        posted by any console is a claim that somebody clicked, not a ruling on
        whether they were allowed to.
        """
        principal = _gate()
        if not _is_safe_id(req_id):
            return jsonify({"error": "unknown intervention"}), 404
        store = HandoffStore(handoff_dir)
        try:
            req = store.read(req_id)
        except Exception:
            return jsonify({"error": "unknown intervention"}), 404
        if req.kind.value != "dual_control":
            return jsonify({"error": "this pause needs someone to take control "
                                     "of the live session, not a "
                                     "counter-signature"}), 409
        if req.status.value != "open":
            return jsonify({"error": f"already {req.status.value}"}), 409
        if principal is None:
            return jsonify({"error": "counter-signing requires a signed-in "
                                     "person; start the console with a session "
                                     "or use `bankcua operator resolve "
                                     "--approver`"}), 401
        if principal.role != "supervisor":
            return jsonify({"error": "a counter-signature must come from a "
                                     "supervisor"}), 403
        if req.initiator and principal.username == req.initiator:
            # The whole point of dual control. Said plainly, because the
            # alternative -- a button that fails with a policy error -- reads
            # as a broken console rather than as the control working.
            return jsonify({"error": f"{principal.username} initiated this run; "
                                     f"a run cannot approve itself. Another "
                                     f"supervisor has to counter-sign it."}), 403
        req.status = InterventionStatus.RESOLVED
        req.resolved_at = time.time()
        req.resolved_by = principal.username
        req.resume = True
        req.controller = "agent"
        req.resolution_note = (f"counter-signed in the console by "
                               f"{principal.username}")
        store.write(req)
        return jsonify({"ok": True, "resolved_by": principal.username})

    @app.get("/api/interventions/<path:req_id>/screenshot")
    def intervention_screenshot(req_id):
        """The frame captured when the run stopped, so a reviewer can see what
        it is being asked to authorise before taking control."""
        principal = _gate()
        if principal is not None and principal.is_member:
            abort(403)
        if not _is_safe_id(req_id):
            abort(404)
        try:
            req = HandoffStore(handoff_dir).read(req_id)
        except Exception:
            abort(404)
        if not req.screenshot_path or not os.path.isfile(req.screenshot_path):
            abort(404)
        return send_file(os.path.abspath(req.screenshot_path))

    @app.get("/api/runs")
    def runs():
        """Newest first, capped.

        Evidence accumulates: a working session leaves dozens of runs behind, and
        an unbounded list buries the handful a reviewer came to look at. The cap
        is applied server-side so the payload stays small, and the total is
        reported so a truncated view never looks like the whole history.
        """
        principal = _gate()
        try:
            limit = max(1, min(int(request.args.get("limit", 60)), 1000))
        except ValueError:
            limit = 60
        rows = _all_runs(principal)
        return jsonify({"total": len(rows), "shown": rows[:limit]})

    @app.get("/api/runs/<run_id>")
    def run(run_id):
        principal = _gate()
        # 404 rather than 403 for a run that exists but is not theirs: telling a
        # member that run `meridian.transfer_funds-...` exists but belongs to
        # someone else is itself a disclosure.
        _root, detail = _find(run_id, principal)
        if detail is None:
            abort(404)
        return jsonify(detail)

    @app.get("/api/runs/<run_id>/evidence/<path:name>")
    def evidence(run_id, name):
        principal = _gate()
        root, detail = _find(run_id, principal)
        if detail is None:
            abort(404)
        # Serve only files this run actually produced. Without the membership
        # check the path is attacker-controlled and `..` walks the filesystem --
        # on a host that also holds screenshots of member accounts.
        if name not in detail["evidence"]:
            abort(404)
        path = os.path.join(root, run_id, name)
        if not os.path.isfile(path):
            abort(404)
        return send_file(os.path.abspath(path))

    return app


def evidence_counts(evidence_dir: str) -> dict[str, int]:
    """Runs per status, for a quick health read."""
    counts: dict[str, int] = {}
    for summary_path in glob.glob(os.path.join(evidence_dir, "*", "summary.json")):
        try:
            with open(summary_path) as fh:
                status = json.load(fh).get("status", "unknown")
        except Exception:
            continue
        counts[status] = counts.get(status, 0) + 1
    return counts


_INDEX_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>bank-cua dashboard</title>
<style>
 :root{color-scheme:dark}
 body{font:13px/1.5 -apple-system,Segoe UI,Arial;margin:0;background:#12161d;color:#e8ecf2}
 header{padding:12px 18px;background:#1f3a5f;display:flex;gap:16px;align-items:baseline}
 header b{font-size:15px}
 .wrap{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px;max-width:1500px;margin:0 auto}
 .card{background:#1a212c;border:1px solid #2a3444;border-radius:8px;padding:14px}
 .card h2{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#8fa3bf}
 table{width:100%;border-collapse:collapse;font-size:12px}
 th{text-align:left;color:#8fa3bf;font-weight:600;padding:5px 6px;border-bottom:1px solid #2a3444}
 td{padding:5px 6px;border-bottom:1px solid #222a36;vertical-align:top}
 tr.run{cursor:pointer} tr.run:hover{background:#222c3a}
 .pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;white-space:nowrap}
 .success{background:#1d4d33;color:#9ff0c4}
 .business{background:#4a3f14;color:#f2dd9b}
 .refused{background:#4a2a14;color:#f2c39b}
 .escalated{background:#3d2a52;color:#dcc0f5}
 .failure{background:#5a2020;color:#f5b0b0}
 .draft{background:#3a3a3a;color:#cfcfcf}
 .approved{background:#1d4d33;color:#9ff0c4}
 code{font-family:ui-monospace,Menlo,monospace;color:#8fd6ac;font-size:11.5px}
 pre{background:#0e131a;border:1px solid #2a3444;border-radius:6px;padding:10px;
     overflow:auto;max-height:340px;font-size:11.5px}
 .full{grid-column:1/-1}
 img{max-width:100%;border:1px solid #2a3444;border-radius:6px;margin-top:8px}
 .muted{color:#8fa3bf}
 .runbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
 select,input{padding:7px;border-radius:5px;border:1px solid #33405a;
              background:#0e131a;color:#e8ecf2;font-size:12px}
 button{background:#2c6b4f;color:#fff;border:0;padding:7px 15px;border-radius:5px;
        cursor:pointer;font-size:12px}
 button:disabled{background:#3a4450;cursor:progress}
 .form{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px}
 .form label{display:block;font-size:11px;color:#8fa3bf;margin-bottom:3px}
 .form input{width:100%}
 .kind{font-size:10px;color:#6f819b;margin-left:5px}
 .banner{padding:10px 12px;border-radius:6px;margin-top:10px;white-space:pre-wrap;
         font-family:ui-monospace,Menlo,monospace;font-size:12px}
</style></head><body>
<header><b>bank-cua</b><span class="muted">MERIDIAN CORE &middot; capabilities, runs and evidence</span>
  <span id="who" class="muted" style="margin-left:auto"></span></header>
<div class="wrap">
  <div class="card full"><h2>Invoke a capability</h2>
    <div class="runbar">
      <select id="cap" onchange="renderForm()"><option>loading…</option></select>
      <select id="op"></select>
      <button id="go" onclick="invoke()">Run</button>
      <span id="runmsg" class="muted"></span>
    </div>
    <div id="form" class="form"></div>
    <div id="result"></div>
  </div>
  <div class="card full" id="escbox" style="display:none;border-color:#6a4a8a">
    <h2>Paused — waiting for a person</h2><div id="esc"></div></div>
  <div class="card"><h2>Capability catalog</h2><div id="caps">loading…</div></div>
  <div class="card"><h2>Run history</h2>
    <div class="runbar">
      <select id="fstatus" onchange="drawRuns()">
        <option value="">all statuses</option>
        <option value="success">success</option>
        <option value="business_outcome">business outcome</option>
        <option value="refused">refused</option>
        <option value="escalated">escalated</option>
        <option value="failure">failure</option>
      </select>
      <select id="fkind" onchange="drawRuns()">
        <option value="">replay + discovery</option>
        <option value="replay">replay only</option>
        <option value="discovery">discovery only</option>
      </select>
      <input id="fcap" placeholder="capability filter" oninput="drawRuns()" style="width:150px">
      <span id="runcount" class="muted"></span>
    </div>
    <div id="runs">loading…</div></div>
  <div class="card full"><h2>Run detail</h2><div id="detail" class="muted">Select a run.</div></div>
</div>
<script>
// This console is mounted under a path when the portal serves it (/dashboard)
// and at the root when the CLI does. Every fetch below is built from this
// rather than from an absolute path, because an absolute /api/... from inside
// the portal's frame hits the PORTAL and returns its sign-in page -- which
// looks, from here, exactly like the API having gone away.
const P="__PREFIX__";
const S={success:['SUCCESS','success'],business_outcome:['BUSINESS OUTCOME','business'],
         refused:['REFUSED','refused'],escalated:['ESCALATED','escalated'],
         failure:['FAILURE','failure']};
function pill(s){const [l,c]=S[s]||[s,'draft'];return `<span class="pill ${c}">${l}</span>`;}
function esc(t){return String(t??'').replace(/[<>&]/g,m=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[m]));}

let MANIFEST=[], ME={};
Promise.all([fetch(P+'/api/manifest').then(r=>r.json()),
             fetch(P+'/api/operators').then(r=>r.json()),
             fetch(P+'/api/me').then(r=>r.json())]).then(([m,ops,me])=>{
  MANIFEST = Array.isArray(m)? m : []; ME = me||{};
  document.getElementById('cap').innerHTML =
    MANIFEST.map(c=>`<option value="${c.name}">${c.name}</option>`).join('')
    || '<option value="">(capability API unreachable)</option>';
  document.getElementById('op').innerHTML =
    (Array.isArray(ops)?ops:[]).map(o=>`<option value="${o.alias}">${o.alias} (${o.role})</option>`).join('');
  // Signed in, the operator is not a choice: the API derives the alias from
  // the session and refuses anything else. Showing a picker that can only be
  // set to one value invites the reading that it could be set to another.
  if(ME.signed_in){
    document.getElementById('op').style.display='none';
    document.getElementById('who').textContent =
      `${ME.display_name} — ${ME.role}` + (ME.runs_as? ` · runs as ${ME.runs_as}`:'');
  }
  renderForm();
});

function renderForm(){
  const tool = MANIFEST.find(c=>c.name===document.getElementById('cap').value);
  if(!tool){ document.getElementById('form').innerHTML=''; return; }
  const props = tool.input_schema.properties||{}, req = tool.input_schema.required||[];
  // Only what a CALLER must decide: the API already filters out credentials and
  // the identity it supplies itself, so there is no password field to render.
  document.getElementById('form').innerHTML = Object.keys(props).length
    ? Object.entries(props).map(([k,v])=>
        `<div><label>${k}${req.includes(k)?' *':''}<span class="kind">${v.type||''}</span></label>
         <input id="p_${k}" placeholder="${esc(v.description||'').slice(0,60)}"></div>`).join('')
    : '<span class="muted">No caller-supplied inputs.</span>';
  document.getElementById('result').innerHTML='';
}

async function invoke(){
  const cap=document.getElementById('cap').value;
  const tool=MANIFEST.find(c=>c.name===cap); if(!tool) return;
  const params={};
  Object.keys(tool.input_schema.properties||{}).forEach(k=>{
    const v=document.getElementById('p_'+k).value.trim(); if(v) params[k]=v; });
  const btn=document.getElementById('go'); btn.disabled=true;
  document.getElementById('runmsg').textContent='running against MERIDIAN CORE…';
  document.getElementById('result').innerHTML='';
  try{
    const r=await fetch(P+'/api/invoke/'+cap,{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({operator:document.getElementById('op').value,params})});
    const d=await r.json();
    const cls=(S[d.status]||['','failure'])[1];
    const detail = d.business_outcome||d.refusal||d.failure||{};
    const bits = [pill(d.status) + ' ' + esc(detail.code||'')];
    if(detail.message||detail.reason) bits.push(esc(detail.message||detail.reason));
    // The actionable half of a refusal: what would have to be true. Kept as
    // separate lines rather than one string with embedded newlines, because
    // this template lives in a non-raw Python literal where a \\n becomes a
    // real line break and silently breaks the whole script.
    if(detail.requirement) bits.push('Would need: ' + esc(detail.requirement));
    if(Object.keys(d.outputs||{}).length) bits.push(esc(JSON.stringify(d.outputs,null,2)));
    document.getElementById('result').innerHTML =
      '<div class="banner ' + cls + '">' + bits.join('<br>') + '</div>';
    document.getElementById('runmsg').textContent='HTTP '+r.status;
    loadRuns();
    if(d.run_id) setTimeout(()=>detailView(d.run_id), 300);
  }catch(e){
    document.getElementById('result').innerHTML='<div class="banner failure">'+e+'</div>';
  }
  btn.disabled=false;
}

fetch(P+'/api/capabilities').then(r=>r.json()).then(cs=>{
  document.getElementById('caps').innerHTML =
   `<table><tr><th>Capability</th><th>State</th><th>Steps</th><th>Outputs</th></tr>` +
   cs.map(c=>`<tr><td><code>${c.id}</code><div class="muted">${esc(c.name)}</div></td>
     <td><span class="pill ${c.approval_state}">${c.approval_state}</span>
     ${c.unreviewed_risky.length?`<div class="muted">${c.unreviewed_risky.length} risky step(s) unreviewed</div>`:''}</td>
     <td>${c.steps}${c.risky_steps.length?` <span class="pill refused">${c.risky_steps.length} risky</span>`:''}</td>
     <td>${c.outputs.map(o=>`${o.name}<span class="muted">:${o.type}</span>`).join('<br>')||'<span class="muted">none</span>'}</td></tr>`).join('') +
   `</table>`;
});

let ALLRUNS=[], RUNTOTAL=0;
function loadRuns(){
 fetch(P+'/api/runs').then(r=>r.json()).then(d=>{
  ALLRUNS = d.shown||[]; RUNTOTAL = d.total||0; drawRuns();
 });
}
function drawRuns(){
  const st=document.getElementById('fstatus').value,
        kd=document.getElementById('fkind').value,
        cap=document.getElementById('fcap').value.toLowerCase();
  const rs = ALLRUNS.filter(r=>(!st||r.status===st)&&(!kd||r.kind===kd)
                              &&(!cap||(r.capability_id||'').toLowerCase().includes(cap)));
  // Say what is NOT shown. A filtered list that looks like the whole history is
  // how someone concludes a failing scenario never ran.
  document.getElementById('runcount').textContent =
    `showing ${rs.length} of ${ALLRUNS.length} loaded (${RUNTOTAL} on disk)`;
  document.getElementById('runs').innerHTML = rs.length?
   `<table><tr><th>Run</th><th>Status</th><th>Steps</th><th>Time</th></tr>` +
   rs.map(r=>`<tr class="run" onclick="detailView('${r.run_id}')">
     <td><code>${r.run_id}</code><div class="muted">${esc(r.capability_id)}</div></td>
     <td>${pill(r.status)}${r.code?`<div class="muted">${esc(r.code)}</div>`:''}</td>
     <td>${r.steps_executed}${r.kind==='discovery'?' <span class="kind">discovery</span>':''}</td>
     <td>${r.duration_s}s</td></tr>`).join('') + `</table>`
   : '<span class="muted">No runs match. Clear the filters, or invoke a capability above.</span>';
}
loadRuns(); setInterval(loadRuns, 4000);

// An escalation is invisible otherwise: the caller's request simply hangs and
// nothing on screen says a human is being waited for. Polled on a short cycle
// because the whole point is to notice it WHILE it is still open.
// Polling must never destroy live UI. The first version rebuilt this panel's
// HTML every 2.5s, which tore out the console iframe an operator was working in
// and replaced it with the still screenshot -- the panel visibly flickered
// between the two while somebody was mid-handoff on a banking screen.
//
// Two guards: re-render only when the rendered STATE actually changes, and
// remember which interventions have a console so a legitimate re-render brings
// the live view back rather than reverting to the still.
let ESC_SIG = '', CONSOLES = {};

function escMarkup(x){
  const cid = cssId(x.id);
  const head = `
    <div>${pill('escalated')} <code>${esc(x.id)}</code>
      &middot; ${esc(x.capability_id||'')}${x.step!=null?' step '+x.step:''}</div>
    <div class="muted" style="margin:4px 0">${esc(x.reason)}</div>
    ${x.url?`<div class="muted">live session: <code>${esc(x.url)}</code></div>`:''}`;

  // A dual-control pause is a DECISION, not a screen. It is raised before the
  // browser has been sent anywhere, so there is nothing to co-browse and
  // nothing to photograph -- the earlier version offered "Take control" here
  // and handed the operator a blank page whose only exit was to abort the run.
  if(x.needs === 'countersignature'){
    const me = ME.username || '';
    const mine = x.initiator && me && x.initiator === me;
    const notSuper = ME.signed_in && ME.role !== 'supervisor';
    let action;
    if(mine){
      action = `<p class="muted">You started this run, so you cannot approve it
        — that is what dual control is. Another supervisor has to counter-sign.</p>`;
    } else if(notSuper){
      action = `<p class="muted">A counter-signature has to come from a supervisor.</p>`;
    } else {
      action = `<p><button onclick="countersign('${esc(x.id)}')">
        Counter-sign as ${esc(me||'the signed-in supervisor')}</button>
        <span class="muted" id="cx_${cid}"></span></p>`;
    }
    return head + `
      <p class="muted">This needs a <b>second person</b> to approve it, not
        someone to drive the screen. Nothing has been opened on the member's
        account${x.initiator?`; the run was started by <code>${esc(x.initiator)}</code>`:''}.</p>`
      + action;
  }

  const ckey = x.id + '@' + x.created_at;
  if(CONSOLES[ckey]){
    return head + `
      <p class="muted">You have control — click the page below, then
        <b>Resume automation</b>.</p>
      <iframe src="${CONSOLES[ckey]}/?e=${encodeURIComponent(ckey)}"
        style="width:100%;height:780px;
        border:1px solid #33405a;border-radius:6px;background:#12161d"></iframe>`;
  }
  if(!x.attachable){
    return head + `<p class="muted">Not attachable: the run exposed no CDP port,
      so no operator can take control of it. Restart the API with
      <code>--cdp-port</code>.</p>`;
  }
  return head + `
    <p><button onclick="takeControl('${esc(x.id)}','${x.created_at}')">Take control of this session</button>
       <span class="muted" id="cs_${cid}"></span></p>`
    + (x.screenshot
        ? `<div class="muted">screen where it stopped:</div>
           <img src="${P}/api/interventions/${encodeURIComponent(x.id)}/screenshot">`
        : `<div class="muted">No frame was captured for this pause.</div>`);
}

function loadEscalations(){
 fetch(P+'/api/interventions').then(r=>r.json()).then(xs=>{
  const box=document.getElementById('escbox');
  if(!xs.length){ box.style.display='none'; ESC_SIG=''; CONSOLES={}; return; }
  box.style.display='';
  // created_at distinguishes THIS escalation from the last one that shared its
  // id, so a console from a previous pause is never shown for a new one.
  const sig = xs.map(x=>x.id+'@'+x.created_at+':'+x.attachable+':'+x.needs).join('|')
            + '#' + Object.keys(CONSOLES).sort().join(',');
  if(sig === ESC_SIG) return;          // nothing changed: leave the DOM alone
  ESC_SIG = sig;
  document.getElementById('esc').innerHTML =
    xs.map(x=>`<div style="margin-bottom:12px">${escMarkup(x)}</div>`).join('');
 });
}
loadEscalations();
const escPoll = setInterval(loadEscalations, 2500);

function cssId(s){ return s.replace(/[^a-zA-Z0-9]/g,'_'); }

// Start the console from here rather than from a terminal. It is the same
// console, over the same CDP seam, recording the same human actions -- the only
// thing that changes is that the most interesting moment of the demo no longer
// requires shelling out.
async function countersign(id){
  const note=document.getElementById('cx_'+cssId(id));
  note.textContent=' recording your approval…';
  try{
    const r=await fetch(P+'/api/interventions/'+encodeURIComponent(id)+'/countersign',
                        {method:'POST'});
    const d=await r.json();
    note.textContent = d.error ? ' '+d.error
                               : ' counter-signed — the run is continuing';
    if(!d.error){ ESC_SIG=''; loadEscalations(); loadRuns(); }
  }catch(e){ note.textContent=' could not counter-sign: '+e; }
}

async function takeControl(id, createdAt){
  const note=document.getElementById('cs_'+cssId(id));
  note.textContent=' starting the console…';
  try{
    const r=await fetch(P+'/api/interventions/'+encodeURIComponent(id)+'/console',
                        {method:'POST'});
    const d=await r.json();
    if(d.error){ note.textContent=' '+d.error; return; }
    note.textContent=' starting…';
    // Record it and let the next poll render the live view. Injecting the
    // iframe here as well would build it twice -- once now, once on the
    // re-render the changed signature triggers -- and reloading a console
    // mid-handoff loses whatever the operator had typed.
    setTimeout(()=>{ CONSOLES[id+'@'+createdAt] = d.url; loadEscalations(); }, 900);
  }catch(e){ note.textContent=' could not start the console: '+e; }
}

function detailView(id){
 fetch(P+'/api/runs/'+id).then(r=>r.json()).then(d=>{
  const s=d.summary, out=s.outputs||{};
  // What the run was ASKED to do, beside what it answered. Taken from the
  // engine's own `replay_started` event rather than from a separate record, so
  // it is the redacted parameter set the run actually received -- a panel fed
  // from anywhere else could disagree with what happened.
  const started=(d.events||[]).find(e=>e.event==='replay_started')||{};
  const inputs=started.params||{};
  const shots=(d.evidence||[]).filter(f=>f.endsWith('.png'));
  const doms=(d.evidence||[]).filter(f=>f.endsWith('.html'));
  document.getElementById('detail').innerHTML =
   `<div>${pill(s.status)} <code>${d.run_id}</code> &middot; ${esc(s.capability_id)} v${esc(s.version)}
      &middot; ${s.steps_executed} steps &middot; ${s.duration_s}s
      ${d.principal&&d.principal.username?`&middot; asked by <code>${esc(d.principal.username)}</code>
        (${esc(d.principal.role)})${d.principal.runs_as?`, ran as <code>${esc(d.principal.runs_as)}</code>`:''}`:''}</div>
    ${s.business_outcome?`<p><b>Outcome:</b> ${esc(s.business_outcome.code)} — ${esc(s.business_outcome.message)}</p>`:''}
    ${s.refusal?`<p><b>Refused:</b> ${esc(s.refusal.code)} — ${esc(s.refusal.reason)}<br>
                 <span class="muted">Would need: ${esc(s.refusal.requirement)}</span></p>`:''}
    ${s.failure?`<p><b>Failure:</b> ${esc(s.failure.code)} at step ${s.failure.step_index}<br>
                 <span class="muted">expected ${esc(s.failure.expected)} / observed ${esc(s.failure.observed)}</span></p>`:''}
    ${(s.recoveries||[]).length?`<p><b>Recovered:</b> ${s.recoveries.map(r=>esc(r.condition_code)+' ×'+r.attempts).join(', ')}</p>`:''}
    ${(s.drifts||[]).length?`<p><b>Locator drift:</b> steps ${s.drifts.map(d=>d.step_index).join(', ')}</p>`:''}
    <h3 style="font-size:12px;color:#8fa3bf">Inputs (redacted)</h3><pre>${esc(JSON.stringify(inputs,null,2))}</pre>
    <h3 style="font-size:12px;color:#8fa3bf">Outputs</h3><pre>${esc(JSON.stringify(out,null,2))}</pre>
    <h3 style="font-size:12px;color:#8fa3bf">Event log (redacted)</h3>
    <pre>${esc((d.events||[]).map(e=>`${e.event} ${JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k])=>!['ts','event'].includes(k))))}`).join('\\n'))}</pre>
    ${doms.length?`<div class="muted">DOM snapshots: ${doms.map(f=>`<a href="${P}/api/runs/${d.run_id}/evidence/${f}" target="_blank"><code>${f}</code></a>`).join(' ')}</div>`:''}
    ${shots.length?`<h3 style="font-size:12px;color:#8fa3bf">Screenshots</h3>`+
       shots.map(f=>`<div><code>${f}</code><img src="${P}/api/runs/${d.run_id}/evidence/${f}"></div>`).join(''):''}`;
  window.scrollTo(0,document.body.scrollHeight);
 });
}
</script></body></html>"""


#: The member-facing console. A separate page rather than the operator page with
#: panels suppressed: the operator page is built around a catalog, an escalation
#: queue and every run on the host, and a version of it that merely HID those
#: would still have fetched them. What a member can see here is what the routes
#: above will serve a member session -- their own record, their own runs -- so
#: the page and the server agree by construction.
_MEMBER_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>My accounts</title>
<style>
 :root{color-scheme:dark}
 body{font:13px/1.55 -apple-system,Segoe UI,Arial;margin:0;background:#12161d;color:#e8ecf2}
 header{padding:12px 18px;background:#1f3a5f;display:flex;gap:16px;align-items:baseline}
 header b{font-size:15px}
 .wrap{max-width:1000px;margin:0 auto;padding:16px;display:grid;gap:16px}
 .card{background:#1a212c;border:1px solid #2a3444;border-radius:8px;padding:14px}
 .card h2{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#8fa3bf}
 table{width:100%;border-collapse:collapse;font-size:12px}
 th{text-align:left;color:#8fa3bf;font-weight:600;padding:5px 6px;border-bottom:1px solid #2a3444}
 td{padding:5px 6px;border-bottom:1px solid #222a36;vertical-align:top}
 .pill{display:inline-block;padding:1px 7px;border-radius:9px;font-size:10.5px;white-space:nowrap}
 .success{background:#1d4d33;color:#9ff0c4}
 .business{background:#4a3f14;color:#f2dd9b}
 .refused{background:#4a2a14;color:#f2c39b}
 .escalated{background:#3d2a52;color:#dcc0f5}
 .failure{background:#5a2020;color:#f5b0b0}
 .muted{color:#8fa3bf}
 code{font-family:ui-monospace,Menlo,monospace;color:#8fd6ac;font-size:11.5px}
 button{background:#2c6b4f;color:#fff;border:0;padding:7px 15px;border-radius:5px;cursor:pointer;font-size:12px}
 button:disabled{background:#3a4450;cursor:progress}
 .banner{padding:10px 12px;border-radius:6px;margin-top:10px;white-space:pre-wrap;
         font-family:ui-monospace,Menlo,monospace;font-size:12px}
 .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
</style></head><body>
<header><b>My accounts</b><span class="muted">MERIDIAN CORE</span>
  <span id="who" class="muted" style="margin-left:auto"></span></header>
<div class="wrap">
  <div class="card"><h2>My record</h2>
    <div class="row"><span id="mid" class="muted"></span>
      <button id="refresh" onclick="refresh()">Refresh from Meridian</button>
      <span id="asof" class="muted"></span></div>
    <div id="details" class="muted" style="margin-top:10px">loading…</div>
    <div id="result"></div>
  </div>
  <div class="card"><h2>What I can do here</h2>
    <div id="perms" class="muted">loading…</div>
    <p class="muted">Anything on this list is limited to my own record. Requests
      about another member are refused by the service, not hidden by this page.</p>
  </div>
  <div class="card"><h2>My activity</h2><div id="runs" class="muted">loading…</div></div>
</div>
<script>
const P="__PREFIX__";
const S={success:['SUCCESS','success'],business_outcome:['BUSINESS OUTCOME','business'],
         refused:['REFUSED','refused'],escalated:['ESCALATED','escalated'],
         failure:['FAILURE','failure']};
function pill(s){const [l,c]=S[s]||[s,'muted'];return `<span class="pill ${c}">${l}</span>`;}
function esc(t){return String(t??'').replace(/[<>&]/g,m=>({'<':'&lt;','>':'&gt;','&':'&amp;'}[m]));}

fetch(P+'/api/me').then(r=>r.json()).then(me=>{
  document.getElementById('who').textContent = `${me.display_name} — member ${me.member_id}`;
  document.getElementById('mid').textContent = `Member ${me.member_id}`;
  document.getElementById('perms').innerHTML = (me.capabilities||[]).length
    ? (me.capabilities||[]).map(c=>`<div><code>${esc(c)}</code></div>`).join('')
    : 'Nothing is enabled for member sign-ins on this deployment.';
});

// A table of rows, a list of strings, or a plain value: the outputs are whatever
// the capability declared it returns, and guessing wrong should degrade to
// showing the value rather than to showing nothing.
function renderOutputs(outputs){
  const parts=[];
  for(const [name,value] of Object.entries(outputs||{})){
    if(Array.isArray(value) && value.length && typeof value[0]==='object'){
      const cols=Object.keys(value[0]);
      parts.push(`<h3 style="font-size:12px;color:#8fa3bf">${esc(name)}</h3><table>`
        + `<tr>${cols.map(c=>`<th>${esc(c)}</th>`).join('')}</tr>`
        + value.map(r=>`<tr>${cols.map(c=>`<td>${esc(r[c])}</td>`).join('')}</tr>`).join('')
        + `</table>`);
    } else if(Array.isArray(value)){
      parts.push(`<div><b>${esc(name)}</b>: ${value.map(esc).join(', ')}</div>`);
    } else {
      parts.push(`<div><b>${esc(name)}</b>: ${esc(value)}</div>`);
    }
  }
  return parts.join('') || '<span class="muted">Nothing recorded yet.</span>';
}

function loadDetails(){
 fetch(P+'/api/me/details').then(r=>r.json()).then(d=>{
  document.getElementById('details').innerHTML = renderOutputs(d.outputs);
  document.getElementById('asof').textContent = d.as_of_run
    ? `as last read from Meridian by run ${d.as_of_run}`
    : 'not read yet — press Refresh';
 });
}

// The one action on this page, and it is a read of the member's OWN record. It
// carries no member number: the service fills that in from the sign-in, which
// is why there is no field here to type someone else's into.
async function refresh(){
  const b=document.getElementById('refresh'); b.disabled=true;
  document.getElementById('result').innerHTML='';
  try{
    const r=await fetch(P+'/api/invoke/meridian.member_lookup',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({params:{}})});
    const d=await r.json();
    const detail=d.business_outcome||d.refusal||d.failure||{};
    const bits=[pill(d.status)+' '+esc(detail.code||'')];
    if(detail.message||detail.reason) bits.push(esc(detail.message||detail.reason));
    document.getElementById('result').innerHTML =
      '<div class="banner '+((S[d.status]||['','failure'])[1])+'">'+bits.join('<br>')+'</div>';
    loadDetails(); loadRuns();
  }catch(e){
    document.getElementById('result').innerHTML='<div class="banner failure">'+esc(e)+'</div>';
  }
  b.disabled=false;
}

function loadRuns(){
 fetch(P+'/api/runs').then(r=>r.json()).then(d=>{
  const rs=d.shown||[];
  document.getElementById('runs').innerHTML = rs.length?
   `<table><tr><th>Request</th><th>Outcome</th><th>When</th></tr>` +
   rs.map(r=>`<tr><td><code>${esc(r.capability_id)}</code>
       <div class="muted">${esc(r.run_id)}</div></td>
     <td>${pill(r.status)}${r.code?`<div class="muted">${esc(r.code)}</div>`:''}</td>
     <td class="muted">${new Date(r.started_at*1000).toLocaleString()}</td></tr>`).join('')
   + `</table>`
   : 'Nothing yet. Anything you ask the assistant to do will appear here.';
 });
}
loadDetails(); loadRuns(); setInterval(loadRuns, 6000);
</script></body></html>"""
