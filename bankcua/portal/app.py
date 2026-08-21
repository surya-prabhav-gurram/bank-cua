"""
The console people actually sign in to: one origin, two tabs, one identity.

What this adds, and what it deliberately does not
-------------------------------------------------
Until now every window onto this system was open: the dashboard and the chatbot
came up unauthenticated and the only subject in the world was an operator ALIAS
chosen from a dropdown. That is fine for a single-operator demo and wrong for
anything else -- a member of the public and a supervisor were the same caller.

This module supplies the sign-in, and NOTHING ELSE. Specifically it does not
supply the enforcement:

  * it mounts the existing dashboard and the existing chatbot unchanged, at
    /dashboard and /assistant, on one origin so a single cookie covers both;
  * it mints the session token (`bankcua/auth.py`) and hands it to the browser;
  * every request the two tabs make carries that token to the capability API,
    which re-resolves it against the principal store and applies the role and
    member-scope rules itself.

So the portal is a door, not a guard. Deleting this file would cost the system
its sign-in page and none of its authorisation: the API refuses a teller's
session asking for a hold, and a member's session asking about somebody else's
account, whether or not anything is mounted in front of it. That split is the
point -- a permission model that lives in the page it decorates is a permission
model that ends at "view source".

What signing in now does to the target
--------------------------------------
Staff pick their operator from a list and press Sign in; the console then asks
the capability API to establish that operator's session on MERIDIAN
(`POST /session/signon`) before it lets them through. That is the whole of
"automated sign-on": nobody types an operator password anywhere, because the
secret lives in the credential store and is merged in server-side, and nobody
has to remember to run a sign-on capability first.

The other half of it is that `meridian.signon` then STOPS EXISTING as far as the
console is concerned -- the API withholds it from the manifest and refuses it at
/invoke for any signed-in caller (`session_signon` in config/service.yaml). The
assistant cannot route to it, the dashboard cannot offer it, and the one thing
that runs it is the act of signing in.

A verdict that is not `success` does not always stop the sign-in, and the
difference matters: if the HOST rejected the credential, that is a definite
answer and the person is not let in. If the sign-on could not be attempted at
all -- API down, target unreachable, capability still in draft -- the console
says so in its header and lets them in unverified, because an offline target is
not evidence about anybody's credential.

Why the staff sign-in takes no password
---------------------------------------
It is a demo affordance, and it is a real hole: anyone who can reach this port
becomes a teller or a supervisor by choosing one from a list. It is written down
here, in the README, and nowhere else does the system rely on it -- every
authorisation decision is still made by the API against the principal store. The
member sign-ins keep their password, because member usernames ARE member
numbers: a passwordless list of them would be an enumeration of the membership
with a login attached to each row.

Why two tabs rather than two ports
----------------------------------
Cookies are scoped to an origin. Serving the dashboard on :8082 and the chatbot
on :8081 would mean either two sign-ins or a token passed between them in a URL
-- and a token in a URL is a token in an access log, a referrer header, and any
screenshot of the address bar. Mounting both under one origin makes the session
a browser fact rather than something the pages have to carry around.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict

from flask import (Flask, jsonify, make_response, redirect, request,
                   render_template_string)
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from ..auth import (SESSION_COOKIE, SESSION_HEADER, STAFF_ROLES, AuthError,
                    Principal, SessionAuthority, ensure_session_secret,
                    token_from_request)

#: Sign-in attempts allowed per username inside the window before it is held
#: shut. Member usernames ARE member numbers, so an unthrottled form is an
#: offline password guess against a known, enumerable account list.
_MAX_ATTEMPTS = 5
_LOCKOUT_S = 60.0


class _Throttle:
    """A crude per-username attempt counter.

    In memory and per process, which is honest about what it is: a speed bump
    for the demo, not a distributed lockout. A real deployment does this at the
    identity provider, which is the same seam `PrincipalStore` names.
    """

    def __init__(self):
        self._fails: dict[str, list[float]] = defaultdict(list)

    def locked(self, username: str, now: float | None = None) -> float:
        now = time.time() if now is None else now
        recent = [t for t in self._fails[username] if now - t < _LOCKOUT_S]
        self._fails[username] = recent
        if len(recent) < _MAX_ATTEMPTS:
            return 0.0
        return _LOCKOUT_S - (now - recent[0])

    def fail(self, username: str) -> None:
        self._fails[username].append(time.time())

    def clear(self, username: str) -> None:
        self._fails.pop(username, None)


#: How long the console waits for the sign-on run. It drives a real browser
#: against a shared host, so it is seconds rather than milliseconds -- but a
#: person is standing at a login form, so it cannot be the API's own 300.
_SIGNON_TIMEOUT_S = 120.0


def post_api(api_url: str, path: str, token: str = "",
             body: dict | None = None,
             timeout: float = _SIGNON_TIMEOUT_S) -> tuple[dict, int]:
    """POST to the capability API as an ordinary client.

    Module-level, and deliberately not a method: the console reaches the API the
    same way anything else does, over HTTP, carrying the session it was just
    handed. There is no in-process shortcut for it to take, which is what keeps
    "the portal is a door, not a guard" true of the sign-on as well.

    A non-2xx is an ANSWER here, not an exception -- a refusal is a 403 with a
    body worth reading -- so nothing is raised for status alone.
    """
    data = json.dumps(body or {}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers[SESSION_HEADER] = token
    req = urllib.request.Request(api_url.rstrip("/") + path, data=data,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), r.status
    except urllib.error.HTTPError as ex:
        try:
            return json.load(ex), ex.code
        except Exception:
            return {"error": ex.reason}, ex.code
    except Exception as ex:
        return {"error": f"capability API unreachable at {api_url}: {ex}"}, 503


def create_app(api_url: str = "http://127.0.0.1:8080",
               catalog_dir: str = "capabilities/meridian",
               evidence_dirs: tuple[str, ...] = ("evidence/service",
                                                 "evidence/meridian"),
               handoff_dir: str = "evidence/handoffs",
               console_port: int = 8090,
               principals_path: str | None = None,
               session_key_path: str = "config/session.key",
               router=None,
               session_authority: SessionAuthority | None = None,
               signon_client=None) -> Flask:
    from ..chat.app import create_app as create_chat
    from ..dashboard import create_app as create_dashboard

    auth = session_authority or SessionAuthority(
        principals_path=principals_path,
        secret_path=session_key_path)
    # Only the console creates the signing key. The API and the dashboard read
    # it: a verifier that can mint the key it trusts will happily verify tokens
    # nobody issued.
    ensure_session_secret(session_key_path)

    app = Flask(__name__)
    app.json.sort_keys = False
    throttle = _Throttle()

    dashboard = create_dashboard(catalog_dir=catalog_dir,
                                 evidence_dirs=tuple(evidence_dirs),
                                 api_url=api_url, handoff_dir=handoff_dir,
                                 console_port=console_port,
                                 session_authority=auth, require_session=True)
    chat = create_chat(api_url=api_url, router=router)

    def _principal() -> Principal | None:
        token = token_from_request(request.headers, request.cookies)
        if not token:
            return None
        try:
            return auth.principal(token)
        except AuthError:
            return None

    #: How the console asks for a sign-on. A seam, not a detail: a test needs
    #: all three verdicts without a live host, and a deployment that reaches its
    #: API over something other than HTTP replaces one callable rather than the
    #: sign-in flow.
    ask_signon = signon_client or (
        lambda token: post_api(api_url, "/session/signon", token))

    #: The last sign-on verdict per username, so the console header can say
    #: whether this session's operator is actually signed on to the target.
    #:
    #: In process and deliberately thin: it holds the RUN ID of the sign-on and
    #: what that run answered, not a second account of it. The evidence the
    #: engine wrote is still the only record, and this is a pointer to it that
    #: disappears when the process does -- which is correct, because a sign-on
    #: verdict about a session cannot outlive the session.
    signons: dict[str, dict] = {}

    def _establish_signon(token: str) -> dict:
        """Sign this session's operator on to the target, and read the verdict.

        Three outcomes, and the middle one is the point of separating them:

          * `signed_on`  -- the host accepted the operator. Nothing else to say.
          * `rejected`   -- the HOST refused this credential. A definite answer
            about this operator, so the console does not let them in.
          * `unverified` -- the sign-on could not be ATTEMPTED: the API is down,
            the target is unreachable, the capability is still in draft, or this
            deployment names no `session_signon` at all. That is not evidence
            about anybody's credential, so it is reported and not treated as a
            refusal -- otherwise an offline target locks every operator out of a
            console whose authorisation does not depend on the target at all.

        The mapping from the result contract is the contract's own: a business
        outcome is the bank answering, a refusal or a failure is not.
        """
        body, status = ask_signon(token)
        body = body if isinstance(body, dict) else {}
        run_id = body.get("run_id") or ""
        if status == 200 and body.get("status") == "success":
            return {"state": "signed_on", "code": "", "run_id": run_id,
                    "detail": "signed on to MERIDIAN", "at": time.time()}
        if status == 200 and body.get("status") == "business_outcome":
            outcome = body.get("business_outcome") or {}
            return {"state": "rejected", "code": outcome.get("code", ""),
                    "run_id": run_id, "at": time.time(),
                    "detail": (outcome.get("message")
                               or outcome.get("code")
                               or "the host declined the sign-on")}
        # A refusal says what a guardrail wanted; a failure says what the run
        # observed. Either is more use to whoever is standing at the login form
        # than the bare status, so the status is only the last resort.
        refusal = body.get("refusal") or {}
        failure = body.get("failure") or {}
        detail = (refusal.get("reason") or failure.get("observed")
                  or body.get("error")
                  or f"the sign-on run ended {body.get('status', status)!r}")
        return {"state": "unverified",
                "code": refusal.get("code", "") or "SIGNON_NOT_ATTEMPTED",
                "run_id": run_id, "detail": detail, "at": time.time()}

    def _signon_badge(principal: Principal | None) -> dict:
        """What the console header says about this session's sign-on."""
        if principal is None or principal.is_member:
            # A member never had an operator session of their own to establish;
            # saying "not verified" at them would describe a thing that does not
            # apply to their sign-in.
            return {}
        badge = dict(signons.get(
            principal.username,
            {"state": "unverified", "code": "SIGNON_NOT_ATTEMPTED",
             "detail": "no sign-on was recorded for this session",
             "run_id": "", "at": 0.0}))
        # One string for the tooltip, built here rather than out of two
        # conditionals inside an HTML attribute -- markup assembled across
        # several template lines is how a rendered phrase silently acquires a
        # line break through the middle of it.
        badge["title"] = (f"{badge['detail']} ({badge['run_id']})"
                          if badge.get("run_id") else badge["detail"])
        return badge

    # ---- who may be picked from the list --------------------------------
    def _staff_signins() -> list[dict]:
        """The operator sign-ins the login page offers, from the store.

        Read from `PrincipalStore` rather than written into the template, so
        adding an operator is an edit to the principal file and not to a page.
        Tellers first: the list's default selection should be the least
        privileged identity that can do the job, not the most.
        """
        out = []
        for name in auth.store.usernames():
            try:
                principal = auth.store.get(name)
            except AuthError:
                continue
            if principal.is_staff and principal.role in STAFF_ROLES:
                out.append({"username": principal.username,
                            "role": principal.role,
                            "display_name": principal.display_name})
        return sorted(out, key=lambda s: (s["role"] != "teller", s["username"]))

    def _render_login(error: str = "", username: str = "", status: int = 200):
        return render_template_string(_LOGIN_PAGE, error=error,
                                      username=username,
                                      staff=_staff_signins()), status

    def _authenticate(username: str, password: str):
        """Establish the principal behind a sign-in attempt, or raise.

        Two paths, and the difference is deliberate. A STAFF sign-in is chosen
        from a short list the deployment configured, and is let in on the choice
        alone -- that is the demo affordance the module docstring owns up to. A
        MEMBER types a password, because member usernames are member numbers: a
        passwordless list of those would be an enumeration of the membership.

        A password supplied for a staff sign-in is still verified rather than
        ignored, so a deployment that hardens this by putting the field back
        gets the check for free.
        """
        if not username:
            raise AuthError("choose a sign-in")
        try:
            principal = auth.store.get(username)
        except AuthError:
            # Never distinguish "no such sign-in" from "wrong password" here;
            # the route renders one message for both.
            raise AuthError("that sign-in and password do not match") from None
        if principal.is_staff and not password:
            return principal, auth.mint(principal)
        return auth.sign_in(username, password)

    # ---- sign in / sign out ---------------------------------------------
    @app.get("/login")
    def login_form():
        page, _status = _render_login()
        return page

    @app.post("/login")
    def login():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        held = throttle.locked(username)
        if held > 0:
            return _render_login(
                username=username,
                error=f"Too many attempts. Try again in {int(held) + 1}s.",
                status=429)
        try:
            principal_record, token = _authenticate(username, password)
        except AuthError as ex:
            throttle.fail(username)
            # One message for a wrong password and for an unknown sign-in. Member
            # usernames are member numbers, so "no such user" would confirm which
            # numbers exist to anyone who asked.
            message = str(ex) if "principal store" in str(ex) else \
                "That sign-in and password do not match."
            return _render_login(username=username, error=message, status=401)
        throttle.clear(username)

        # ---- the sign-on itself, before anyone is let through -------------
        if principal_record.is_staff:
            verdict = _establish_signon(token)
            if verdict["state"] == "rejected":
                # The HOST answered about this credential, and the answer was
                # no. Letting someone into a console whose every capability
                # begins by signing on with that credential would only move the
                # failure to their first request, with less to read.
                return _render_login(
                    username=username, status=401,
                    error=f"MERIDIAN refused that operator: {verdict['detail']}")
            signons[principal_record.username] = verdict
        # Only ever a path on this console. `?next=https://elsewhere/` would
        # turn the sign-in page into an open redirect, which is a credible
        # phishing landing spot precisely BECAUSE it is a real bank console URL.
        target = request.args.get("next") or "/"
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        response = make_response(redirect(target))
        response.set_cookie(
            SESSION_COOKIE, token,
            httponly=True,          # a token JavaScript can read is a token an
                                    # injected script can post somewhere
            samesite="Lax",         # blocks the cross-site form post that would
                                    # otherwise act with this session
            secure=request.is_secure,
            path="/")
        return response

    @app.post("/logout")
    def logout():
        principal = _principal()
        if principal is not None:
            # The verdict described a session that no longer exists. Keeping it
            # would let the next sign-in inherit the last one's green badge
            # without anything having signed on.
            signons.pop(principal.username, None)
        response = make_response(redirect("/login"))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/me")
    def me():
        principal = _principal()
        if principal is None:
            return jsonify({"signed_in": False}), 401
        return jsonify({"signed_in": True, **principal.to_public(),
                        "signon": _signon_badge(principal)})

    # ---- the shell -------------------------------------------------------
    @app.get("/")
    def shell():
        principal = _principal()
        if principal is None:
            return redirect("/login")
        return render_template_string(_SHELL_PAGE, me=principal.to_public(),
                                      signon=_signon_badge(principal))

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    # ---- the two tabs, mounted --------------------------------------------
    def gate(inner):
        """Refuse an unauthenticated request before it reaches a mounted app.

        Belt and braces rather than the belt itself: the dashboard is started
        with `require_session=True` and the API re-checks every invocation
        independently. What this adds is that an expired session gets the
        sign-in page instead of a 401 rendered inside a frame, which is the
        difference between "please sign in again" and "the console is broken".
        """
        def middleware(environ, start_response):
            token = (environ.get("HTTP_" + SESSION_HEADER.upper().replace("-", "_"))
                     or _cookie(environ.get("HTTP_COOKIE", ""), SESSION_COOKIE))
            valid = False
            if token:
                try:
                    auth.principal(token)
                    valid = True
                except AuthError:
                    valid = False
            if not valid:
                start_response("302 Found", [("Location", "/login"),
                                             ("Content-Type", "text/plain")])
                return [b"sign in"]
            # Hand the token on as a header so a mounted app never has to know
            # about this console's cookie name to reach the API as the caller.
            environ[f"HTTP_{SESSION_HEADER.upper().replace('-', '_')}"] = token
            return inner(environ, start_response)
        return middleware

    app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
        "/dashboard": gate(dashboard.wsgi_app),
        "/assistant": gate(chat.wsgi_app),
    })
    app.config["bankcua_dashboard"] = dashboard
    app.config["bankcua_chat"] = chat
    app.config["bankcua_auth"] = auth
    return app


def _cookie(header: str, name: str) -> str:
    for part in header.split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value.strip()
    return ""


def init_files(principals_path: str = "config/principals.json",
               session_key_path: str = "config/session.key",
               example_path: str = "config/principals.example.json") -> list[str]:
    """Make the console runnable: a signing key, and a principal store.

    Copies the example rather than inventing sign-ins, so what a reader gets is
    the file they can read, edit and re-hash -- and so nothing here silently
    creates an account nobody documented.
    """
    created = []
    if not os.path.exists(session_key_path):
        ensure_session_secret(session_key_path)
        created.append(session_key_path)
    if not os.path.exists(principals_path) and os.path.exists(example_path):
        with open(example_path) as src:
            body = src.read()
        os.makedirs(os.path.dirname(os.path.abspath(principals_path)),
                    exist_ok=True)
        fd = os.open(principals_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as dst:
            dst.write(body)
        created.append(principals_path)
    return created


_LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>bank-cua console — sign in</title>
<style>
 :root{color-scheme:dark}
 body{font:14px/1.55 -apple-system,Segoe UI,Arial;margin:0;background:#12161d;
      color:#e8ecf2;display:grid;place-items:center;min-height:100vh}
 .box{background:#1a212c;border:1px solid #2a3444;border-radius:10px;
      padding:26px 28px;width:340px}
 h1{font-size:16px;margin:0 0 4px}
 .sub{color:#8fa3bf;font-size:12px;margin-bottom:18px}
 label{display:block;font-size:11px;color:#8fa3bf;margin:12px 0 4px}
 input,select{width:100%;padding:9px;border-radius:6px;border:1px solid #33405a;
       background:#0e131a;color:#e8ecf2;font-size:13px;box-sizing:border-box}
 button{margin-top:18px;width:100%;background:#2c6b4f;color:#fff;border:0;
        padding:10px;border-radius:6px;cursor:pointer;font-size:13px}
 .err{margin-top:14px;background:#5a2020;color:#f5b0b0;padding:9px 11px;
      border-radius:6px;font-size:12px}
 .note{color:#6f819b;font-size:11px;margin-top:16px;line-height:1.5}
 .rule{display:flex;align-items:center;gap:10px;margin:22px 0 4px;
       color:#5c6b83;font-size:10.5px;text-transform:uppercase;
       letter-spacing:.08em}
 .rule:before,.rule:after{content:"";flex:1;height:1px;background:#2a3444}
 details summary{cursor:pointer;color:#8fa3bf;font-size:12px;margin-top:6px}
 details[open] summary{margin-bottom:2px}
 .warn{color:#c9a24a;font-size:11px;margin-top:10px;line-height:1.5}
</style></head><body>
<div class="box">
  <h1>bank-cua console</h1>
  <div class="sub">MERIDIAN CORE &middot; sign in to continue</div>

  <form method="post" action="/login">
    <label for="username">Operator</label>
    <select id="username" name="username" autofocus>
      {% for s in staff %}
      <option value="{{ s.username }}"
              {% if s.username == username %}selected{% endif %}>
        {{ s.display_name }} &middot; {{ s.role }}</option>
      {% endfor %}
    </select>
    <button type="submit">Sign in</button>
  </form>

  {% if error %}<div class="err">{{ error }}</div>{% endif %}

  <div class="note">Choosing an operator signs that operator on to MERIDIAN
    before the console opens. No operator password is typed here or held by this
    page: the secret lives in the deployment's credential store and is supplied
    server-side at the moment of use.</div>
  <div class="warn">Demo affordance: staff sign in on the choice alone. Anyone
    who can reach this port can become either operator.</div>

  <div class="rule">or a member</div>
  <form method="post" action="/login">
    <label for="member">Member number</label>
    <input id="member" name="username" autocomplete="username"
           inputmode="numeric">
    <label for="password">Password</label>
    <input id="password" name="password" type="password"
           autocomplete="current-password">
    <button type="submit">Sign in</button>
  </form>
  <div class="note">A member is not a Meridian operator, so nothing is signed on
    for them: their work runs delegated on the deployment's least-privileged
    staff alias and is bound to their own member number. Their password is
    required — member numbers are guessable, so a list to pick from would be an
    enumeration of the membership.</div>
</div>
<script>
 // Served inside the console's frame when a session expires mid-session. Break
 // out, or the sign-in form renders in a panel inside a page that is no longer
 // signed in -- and signing in there leaves the shell stale.
 if (window.top !== window.self) window.top.location = '/login';
</script>
</body></html>"""


_SHELL_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>bank-cua console</title>
<style>
 :root{color-scheme:dark}
 html,body{height:100%}
 body{font:13px/1.5 -apple-system,Segoe UI,Arial;margin:0;background:#12161d;
      color:#e8ecf2;display:flex;flex-direction:column}
 header{padding:0 16px;background:#1f3a5f;display:flex;gap:18px;align-items:center;
        flex:0 0 46px}
 header b{font-size:14px}
 .tabs{display:flex;gap:4px;margin-left:8px}
 .tab{padding:7px 16px;border-radius:6px 6px 0 0;background:#18293f;color:#a9c2e0;
      border:0;cursor:pointer;font-size:13px}
 .tab.on{background:#12161d;color:#e8ecf2}
 .who{margin-left:auto;color:#a9c2e0;font-size:12px;display:flex;gap:12px;
      align-items:center}
 .role{background:#12161d;border-radius:9px;padding:2px 9px;font-size:11px}
 .signon{border-radius:9px;padding:2px 9px;font-size:11px}
 .signon.signed_on{background:#1d4d33;color:#9ff0c4}
 .signon.unverified{background:#4a3f14;color:#f2dd9b}
 form{margin:0}
 .out{background:#2a3d57;color:#dbe7f5;border:0;padding:6px 12px;border-radius:5px;
      cursor:pointer;font-size:12px}
 .panes{flex:1;position:relative}
 iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:#12161d}
 iframe.off{display:none}
</style></head><body>
<header>
  <b>bank-cua</b>
  <div class="tabs">
    <button class="tab on" id="t_dash" onclick="show('dash')">Dashboard</button>
    <button class="tab" id="t_chat" onclick="show('chat')">Assistant</button>
  </div>
  <div class="who">
    <span>{{ me.display_name }}</span>
    <span class="role">{{ me.role }}{% if me.member_id %} · {{ me.member_id }}{% endif %}</span>
    {% if signon %}
    <span class="signon {{ signon.state }}" title="{{ signon.title }}"
      >{{ 'MERIDIAN signed on' if signon.state == 'signed_on'
          else 'MERIDIAN not verified' }}</span>
    {% endif %}
    <form method="post" action="/logout"><button class="out">Sign out</button></form>
  </div>
</header>
<div class="panes">
  <iframe id="dash" src="/dashboard/"></iframe>
  <iframe id="chat" class="off" src="/assistant/"></iframe>
</div>
<script>
// Both frames stay loaded and only their visibility changes: switching tabs
// mid-conversation must not throw away what the assistant has already said, and
// reloading the dashboard would drop whichever run detail was open.
function show(which){
  for(const id of ['dash','chat']){
    document.getElementById(id).classList.toggle('off', id!==which);
    document.getElementById('t_'+id).classList.toggle('on', id===which);
  }
}
</script>
</body></html>"""
