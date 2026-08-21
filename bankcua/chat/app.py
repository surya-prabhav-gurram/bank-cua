"""
The conversational front door: a thin driver over the capability API.

Deliberately thin, and deliberately over HTTP
--------------------------------------------
This process imports nothing from `bankcua` except the router and the presenter.
It reaches capabilities only through the published API, exactly as any other
agent would. That is not tidiness -- it is what makes "the chatbot cannot reach
past the API" a structural fact rather than a promise, and it is asserted in
tests/test_chat.py.

Consequences worth being explicit about, because they are the safety story:

  * it never holds a password -- it names an operator ALIAS and the service
    resolves the secret;
  * it cannot grant itself permission -- `allow_risky` and the approval gate are
    server-side, so nothing it sends changes what it is allowed to do;
  * it cannot invent an interaction -- the catalog is its entire action space.

  * it cannot decide WHO is asking -- it forwards the session token the console
    gave the browser and the API re-resolves it, so the answer to "may this
    person do this, and to whose account" is settled somewhere this process
    cannot reach.

A wrong routing decision therefore produces a wrong QUESTION, answered safely.

That last point is what makes a member-facing assistant defensible. The manifest
this app routes over is already narrowed by the API to what the signed-in person
may invoke, so a member's assistant is not offered a hold or a member search at
all; and if it invented one anyway, the invocation would be refused. The action
space shrinks with the sign-in, and it shrinks server-side.

The seam: swap this front door (Slack, voice, a real agent) without touching the
API, the capabilities, or the engine.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from flask import Flask, Response, jsonify, request

from .presenter import present
from .router import Router, RuleRouter

#: The session header, spelled out rather than imported from `bankcua.auth`.
#: This process reaches the rest of the system only over HTTP, and a header name
#: is part of that wire contract -- importing it would be the first thread of a
#: dependency this app is deliberately without (tests/test_chat.py asserts it).
SESSION_HEADER = "X-Bankcua-Session"
SESSION_COOKIE = "bankcua_session"


class CapabilityClient:
    """HTTP client for the capability API. The only way this app reaches anything."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _get(self, path: str, token: str = ""):
        req = urllib.request.Request(
            self.base_url + path,
            headers={SESSION_HEADER: token} if token else {})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)

    def manifest(self, token: str = "") -> list[dict]:
        """The action space, as the SIGNED-IN person may see it.

        Passing the session here is not a convenience: the manifest is the whole
        set of things this front door can decide to do, so narrowing it by who
        is asking is what stops a member's assistant from ever considering a
        capability that is not theirs to use.
        """
        return self._get("/capabilities", token)

    def interventions(self, token: str = "") -> list[dict]:
        """Runs currently stopped, waiting for a person. [] if the API cannot say.

        Polled while a request is in flight, so a failure here must be quiet:
        the run itself is still going, and an error banner about the pause list
        would describe a problem the person cannot act on.
        """
        try:
            return self._get("/interventions", token)
        except Exception:
            return []

    def confirm(self, req_id: str, token: str = "") -> tuple[dict, int]:
        """Authorise a paused irreversible step. The API decides who may."""
        headers = {"Content-Type": "application/json"}
        if token:
            headers[SESSION_HEADER] = token
        req = urllib.request.Request(
            f"{self.base_url}/interventions/{req_id}/confirm", data=b"{}",
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r), r.status
        except urllib.error.HTTPError as ex:
            try:
                return json.load(ex), ex.code
            except Exception:
                return {"error": f"HTTP {ex.code} {ex.reason}"}, ex.code
        except Exception as ex:
            return {"error": f"the capability API is not reachable ({ex})"}, 503

    def session(self, token: str) -> dict:
        """Who the API says is signed in. Returns {} when nobody is."""
        try:
            return self._get("/session", token)
        except Exception:
            return {}

    def invoke(self, cap_id: str, params: dict, operator: str | None,
               token: str = "") -> dict:
        # `channel` says WHERE this run was asked for. The API stamps it on any
        # intervention the run raises, which is what lets a pause be shown to
        # the person who is actually waiting on it rather than only on an
        # operator queue in another tab.
        body = json.dumps({"params": params, "channel": "assistant",
                           **({"operator": operator} if operator else {})}).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers[SESSION_HEADER] = token
        req = urllib.request.Request(
            f"{self.base_url}/invoke/{cap_id}", data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as ex:
            # A refusal is a 403 and a business outcome is a 200: both are
            # ANSWERS, so the body is what matters, not the status line. Treating
            # a non-2xx as an exception here would collapse "the bank declined"
            # into "the call failed", which is the distinction this whole system
            # is built to preserve.
            try:
                return json.load(ex)
            except Exception:
                # Not every error body is the contract: a crash in the service,
                # or a proxy in front of it, answers in HTML. Parsing that as
                # JSON would take the chatbot down alongside the API instead of
                # reporting that the API is what broke.
                return {"status": "failure", "capability_id": cap_id,
                        "failure": {"code": "API_ERROR",
                                    "expected": "the capability API to answer "
                                                "with the result contract",
                                    "observed": f"HTTP {ex.code} {ex.reason}"}}


def create_app(api_url: str = "http://127.0.0.1:8080",
               router: Router | None = None,
               default_operator: str = "teller1") -> Flask:
    app = Flask(__name__)
    # A grid's column order is part of its meaning -- "Share ID, Type,
    # Balance, Status" is how the operator reads the screen. Flask sorts
    # JSON keys by default, which silently alphabetises every extracted
    # table on the way out.
    app.json.sort_keys = False
    client = CapabilityClient(api_url)
    route = router or RuleRouter()

    def _token() -> str:
        """The session, from the header the console injects or the cookie the
        browser sends. Never from the message body: a chat message is written by
        whoever is typing, and an identity they can type is not an identity."""
        header = request.headers.get(SESSION_HEADER) or ""
        if not header:
            auth = request.headers.get("Authorization") or ""
            if auth.lower().startswith("bearer "):
                header = auth[7:].strip()
        return (header or request.cookies.get(SESSION_COOKIE) or "").strip()

    @app.get("/")
    def index():
        # Served under /assistant by the portal and at the root by the CLI, so
        # the page builds its own URLs from the mount point rather than from an
        # absolute path that would leave the frame.
        return _PAGE.replace("__PREFIX__", request.script_root or "")

    @app.get("/whoami")
    def whoami():
        """What the API says about the signed-in person, for the header and the
        suggestions. This app asks rather than decides -- it holds no principal
        store and could not check a password if it wanted to."""
        return jsonify(client.session(_token()) or {"signed_in": False})

    @app.get("/interventions")
    def interventions():
        """Pauses this conversation raised, for the page to poll while it waits.

        Narrowed to `channel == "assistant"`: the operator queue is the
        dashboard's job, and a chat window listing runs somebody else started
        from another tab would be showing its user other people's work.
        """
        return jsonify([i for i in client.interventions(_token())
                        if i.get("channel") == "assistant"])

    @app.post("/interventions/<path:req_id>/confirm")
    def confirm(req_id):
        """Approve a paused step from the chat window.

        A proxy and nothing else -- the API re-resolves the session, checks the
        role and refuses a dual-control pause itself. This app could not
        authorise anything if it wanted to: it holds no principal store, and
        `tests/test_chat.py` asserts it imports nothing from `bankcua`.
        """
        body, status = client.confirm(req_id, _token())
        return jsonify(body), status

    @app.get("/interventions/<path:req_id>/screenshot")
    def screenshot(req_id):
        """The screen the run stopped on, streamed through from the API.

        Approving what you cannot see is a rubber stamp, and the whole reason to
        show the pause here rather than only on the dashboard is that the person
        deciding is the one who asked.
        """
        token = _token()
        req = urllib.request.Request(
            f"{api_url.rstrip('/')}/interventions/{req_id}/screenshot",
            headers={SESSION_HEADER: token} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return Response(r.read(), mimetype=r.headers.get(
                    "Content-Type", "image/png"))
        except Exception:
            return jsonify({"error": "no screenshot"}), 404

    @app.post("/chat")
    def chat():
        body = request.get_json(force=True, silent=True) or {}
        message = (body.get("message") or "").strip()
        token = _token()
        # An explicit operator only when nobody is signed in. With a session,
        # the API derives the alias itself and refuses a request that names a
        # different one -- so sending one here could only ever be wrong.
        operator = None if token else (body.get("operator") or default_operator)
        if not message:
            return jsonify({"reply": "Ask me something.", "detail": None})

        try:
            manifest = client.manifest(token)
        except Exception as ex:
            return jsonify({"reply": f"The capability API is not reachable "
                                     f"({ex}). Start it with "
                                     f"`python -m bankcua.cli serve`.",
                            "detail": None})

        routed = route.route(message, manifest)
        if not routed.capability_id:
            return jsonify({"reply": routed.unmatched_reason, "detail": None})
        if routed.missing:
            # Asking is the correct behaviour, not a limitation: guessing a
            # member number or an amount is the one thing this must never do.
            # But say WHY when something was offered and rejected, or the reply
            # reads as though nothing was given and the user retypes it.
            reasons = [routed.rejected[k] for k in routed.missing
                       if k in routed.rejected]
            need = ", ".join(k for k in routed.missing if k not in routed.rejected)
            parts = []
            if reasons:
                parts.append(" ".join(reasons) + ".")
            if need:
                parts.append(f"I can run **{routed.capability_id}**, but I still "
                             f"need: {need}.")
            return jsonify({
                "reply": " ".join(parts),
                "detail": {"capability_id": routed.capability_id,
                           "params": routed.params, "missing": routed.missing}})

        try:
            result = client.invoke(routed.capability_id, routed.params,
                                   routed.operator or operator, token)
        except Exception as ex:
            # The manifest fetch above already succeeded, so the API was up a
            # moment ago. Say what actually happened rather than rendering a
            # stack trace at the person who asked about a member's balance.
            return jsonify({"reply": f"The capability API stopped responding "
                                     f"part-way through that call ({ex}). "
                                     f"Whether it took effect has to be "
                                     f"confirmed before retrying.",
                            "capability_id": routed.capability_id,
                            "detail": None})
        return jsonify({"reply": present(result),
                        "capability_id": routed.capability_id,
                        "params": routed.params,
                        "status": result.get("status"),
                        "run_id": result.get("run_id"),
                        "detail": result})

    return app


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Meridian assistant</title>
<style>
 :root{color-scheme:dark}
 body{font:14px/1.55 -apple-system,Segoe UI,Arial;margin:0;background:#12161d;color:#e8ecf2}
 header{padding:12px 18px;background:#1f3a5f;display:flex;gap:14px;align-items:center}
 header b{font-size:15px}
 .wrap{max-width:860px;margin:0 auto;padding:18px}
 .msg{margin:10px 0;padding:11px 14px;border-radius:8px;white-space:pre-wrap}
 .me{background:#24405f;margin-left:12%}
 .bot{background:#1a212c;border:1px solid #2a3444;font-family:ui-monospace,Menlo,monospace;font-size:12.5px}
 .meta{font-size:11px;color:#8fa3bf;margin-top:6px}
 .pill{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;margin-right:6px}
 .success{background:#1d4d33;color:#9ff0c4}
 .business_outcome{background:#4a3f14;color:#f2dd9b}
 .refused{background:#4a2a14;color:#f2c39b}
 .escalated{background:#3d2a52;color:#dcc0f5}
 .failure{background:#5a2020;color:#f5b0b0}
 form{display:flex;gap:8px;margin-top:16px}
 input,select{padding:9px;border-radius:6px;border:1px solid #33405a;background:#0e131a;color:#e8ecf2}
 input[type=text]{flex:1}
 button{background:#2c6b4f;color:#fff;border:0;padding:9px 16px;border-radius:6px;cursor:pointer}
 .hint{color:#8fa3bf;font-size:12px;margin-top:14px}
 code{color:#8fd6ac}
 .paused{background:#241a33;border:1px solid #6a4a8a;border-radius:8px;
         padding:12px 14px;margin:10px 0}
 .paused h4{margin:0 0 6px;font-size:12px;text-transform:uppercase;
            letter-spacing:.06em;color:#dcc0f5}
 .paused img{max-width:100%;border:1px solid #3a3050;border-radius:6px;
             margin-top:10px}
 .paused button{margin-top:10px}
 .paused .why{color:#c6b3dd;font-size:12.5px}
 .waiting{color:#8fa3bf;font-style:italic}
</style></head><body>
<header><b>Meridian assistant</b>
  <span style="font-size:12px;color:#a9c2e0">a thin driver over the capability API</span>
  <span id="who" style="font-size:12px;color:#a9c2e0;margin-left:auto"></span></header>
<div class="wrap">
  <div id="log"></div>
  <form onsubmit="send(event)">
    <!-- There is deliberately no operator picker here. Who this assistant acts
         as is decided by the sign-in, server-side: the API derives the alias
         from the session and refuses a request that names a different one. A
         dropdown could therefore only ever offer a choice that is ignored when
         somebody is signed in, or one that is refused when they pick the other
         name -- which reads as a broken control rather than as the guardrail
         working. Unattended callers (no sign-in) get the alias the deployment
         configured with `chat --operator`. -->
    <input type="text" id="msg" placeholder="e.g. what are the balances for member 100234?" autofocus>
    <button>Send</button>
  </form>
  <div class="hint" id="hint">Try: <code>balances for member 100234</code> &middot;
    <code>find members named Turing</code> &middot;
    <code>transfer 1.00 from 100234-S0070 to 100234-S0001-3</code> &middot;
    <code>place a fraud hold on 100234-S0070</code> (supervisors only — a teller
    sign-in is refused before a browser opens)</div>
</div>
<script>
const P="__PREFIX__";

// Who is signed in shapes the SUGGESTIONS and nothing else -- suggesting a
// member search to a member is suggesting something that will be refused. It
// shapes no permission: what this assistant may do is decided by the API from
// the session on every single call, whatever this page believes.
fetch(P+'/whoami').then(r=>r.json()).then(me=>{
  if(!me || !me.username) return;
  document.getElementById('who').textContent =
    `${me.display_name||me.username} — ${me.role}` +
    (me.runs_as? ` · runs as ${me.runs_as}`:'');
  const caps=(me.capabilities||[]);
  const box=document.getElementById('msg');
  if(me.kind==='member'){
    box.placeholder='e.g. what are my balances?';
    document.getElementById('hint').innerHTML =
      'Everything here is limited to member <code>'+me.member_id+'</code>. Try: '+
      '<code>what are my balances?</code> &middot; '+
      '<code>transfer 1.00 from '+me.member_id+'-S0070 to '+me.member_id+'-S0001-3</code> &middot; '+
      '<code>balances for member 100987</code> (to see the refusal)<br>' +
      'Available to you: ' + (caps.length? caps.map(c=>'<code>'+c+'</code>').join(' '): 'nothing');
  } else {
    document.getElementById('hint').innerHTML +=
      '<br>Available to you: ' + (caps.length? caps.map(c=>'<code>'+c+'</code>').join(' '): 'nothing');
  }
});

function add(cls, text, meta){
  const d=document.createElement('div'); d.className='msg '+cls; d.textContent=text;
  if(meta){ const m=document.createElement('div'); m.className='meta'; m.innerHTML=meta; d.appendChild(m); }
  document.getElementById('log').appendChild(d); window.scrollTo(0,document.body.scrollHeight);
}
// A run that stops for a person does it INSIDE the /chat call: the request is
// still open, waiting, while the automation holds a live banking screen. Until
// now the only sign of that in here was a reply that took ninety seconds to
// arrive, while the question -- "may I post this?" -- was being asked on a
// different tab. So while a call is in flight, poll for the pauses THIS
// conversation raised and put them in the transcript where the person who
// caused one is already looking.
let POLL=null;
// Built with DOM calls rather than an HTML string, and the handler attached
// in code rather than as an onclick attribute. Not a style preference: this
// markup lives inside a Python triple-quoted template, so every nested quote
// gets un-escaped twice on its way to the browser -- and a single stray
// apostrophe closes a JS string, which takes the WHOLE script down and leaves a
// page whose input box silently does nothing. Text set with textContent also
// cannot smuggle markup out of a reason string the host wrote.
function renderPause(x){
  if(document.getElementById('pause_'+x.id)) return;   // do not duplicate
  const el=document.createElement('div');
  el.className='paused'; el.id='pause_'+x.id;

  const head=document.createElement('h4');
  head.textContent='Paused — waiting for you';
  const why=document.createElement('div');
  why.className='why'; why.textContent=x.reason;
  el.appendChild(head); el.appendChild(why);

  if(x.kind === 'dual_control'){
    const note=document.createElement('div');
    note.className='why'; note.style.marginTop='8px';
    note.textContent='This one needs a SECOND supervisor to counter-sign — '+
      'you cannot approve your own run. It is in the operator queue on the '+
      'dashboard, for somebody else to answer.';
    el.appendChild(note);
  } else {
    const row=document.createElement('p');
    const btn=document.createElement('button');
    btn.textContent='Confirm and continue';
    const msg=document.createElement('span');
    msg.className='meta';
    btn.addEventListener('click', ()=>confirmPause(x.id, btn, msg));
    row.appendChild(btn); row.appendChild(msg);
    const note=document.createElement('div');
    note.className='why'; note.style.marginTop='8px';
    note.textContent='Approves the step as it stands; the automation posts it. '+
      'Nothing has been submitted yet.';
    el.appendChild(row); el.appendChild(note);
  }

  if(x.has_screenshot){
    const img=document.createElement('img');
    img.src=P+'/interventions/'+encodeURIComponent(x.id)+'/screenshot';
    img.alt='the screen the run stopped on';
    el.appendChild(img);
  }
  document.getElementById('log').appendChild(el);
  window.scrollTo(0,document.body.scrollHeight);
}

async function confirmPause(id, btn, note){
  note.textContent=' recording your approval…';
  try{
    const r=await fetch(P+'/interventions/'+encodeURIComponent(id)+'/confirm',
                        {method:'POST'});
    const d=await r.json();
    if(d.error || d.refusal){
      note.textContent=' '+(d.error || (d.refusal && d.refusal.reason));
      return;
    }
    btn.remove();
    note.textContent=' approved by '+d.resolved_by+' — the run is continuing';
  }catch(err){ note.textContent=' could not confirm: '+err; }
}

function startPolling(){
  stopPolling();
  POLL=setInterval(async ()=>{
    try{
      const r=await fetch(P+'/interventions');
      (await r.json()).forEach(renderPause);
    }catch(e){ /* the run is still going; a poll failure is not something the person
                  problem and must not become a banner */ }
  }, 1500);
}
function stopPolling(){ if(POLL){ clearInterval(POLL); POLL=null; } }

async function send(e){
  e.preventDefault();
  const box=document.getElementById('msg'), text=box.value.trim();
  if(!text) return;
  add('me', text); box.value=''; box.disabled=true;
  const waiting=document.createElement('div');
  waiting.className='msg bot waiting'; waiting.textContent='working…';
  document.getElementById('log').appendChild(waiting);
  startPolling();
  try{
    const r=await fetch(P+'/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text})});
    const d=await r.json();
    let meta='';
    if(d.status) meta += '<span class="pill '+d.status+'">'+d.status+'</span>';
    if(d.capability_id) meta += d.capability_id;
    if(d.run_id) meta += ' &middot; run <code>'+d.run_id+'</code>';
    waiting.remove();
    add('bot', d.reply, meta);
  }catch(err){ waiting.remove();
               add('bot','The assistant could not reach its own backend: '+err); }
  stopPolling();
  box.disabled=false; box.focus();
}
</script></body></html>"""
