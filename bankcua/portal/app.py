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

Why two tabs rather than two ports
----------------------------------
Cookies are scoped to an origin. Serving the dashboard on :8082 and the chatbot
on :8081 would mean either two sign-ins or a token passed between them in a URL
-- and a token in a URL is a token in an access log, a referrer header, and any
screenshot of the address bar. Mounting both under one origin makes the session
a browser fact rather than something the pages have to carry around.
"""
from __future__ import annotations

import os
import time
from collections import defaultdict

from flask import (Flask, jsonify, make_response, redirect, request,
                   render_template_string)
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from ..auth import (SESSION_COOKIE, SESSION_HEADER, AuthError, Principal,
                    SessionAuthority, ensure_session_secret,
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


def create_app(api_url: str = "http://127.0.0.1:8080",
               catalog_dir: str = "capabilities/meridian",
               evidence_dirs: tuple[str, ...] = ("evidence/service",
                                                 "evidence/meridian"),
               handoff_dir: str = "evidence/handoffs",
               console_port: int = 8090,
               principals_path: str | None = None,
               session_key_path: str = "config/session.key",
               router=None,
               session_authority: SessionAuthority | None = None) -> Flask:
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

    # ---- sign in / sign out ---------------------------------------------
    @app.get("/login")
    def login_form():
        return render_template_string(_LOGIN_PAGE, error="", username="")

    @app.post("/login")
    def login():
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        held = throttle.locked(username)
        if held > 0:
            return render_template_string(
                _LOGIN_PAGE, username=username,
                error=f"Too many attempts. Try again in {int(held) + 1}s."), 429
        try:
            _principal_record, token = auth.sign_in(username, password)
        except AuthError as ex:
            throttle.fail(username)
            # One message for a wrong password and for an unknown sign-in. Member
            # usernames are member numbers, so "no such user" would confirm which
            # numbers exist to anyone who asked.
            message = str(ex) if "principal store" in str(ex) else \
                "That sign-in and password do not match."
            return render_template_string(_LOGIN_PAGE, username=username,
                                          error=message), 401
        throttle.clear(username)
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
        response = make_response(redirect("/login"))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/me")
    def me():
        principal = _principal()
        if principal is None:
            return jsonify({"signed_in": False}), 401
        return jsonify({"signed_in": True, **principal.to_public()})

    # ---- the shell -------------------------------------------------------
    @app.get("/")
    def shell():
        principal = _principal()
        if principal is None:
            return redirect("/login")
        return render_template_string(_SHELL_PAGE, me=principal.to_public())

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
 input{width:100%;padding:9px;border-radius:6px;border:1px solid #33405a;
       background:#0e131a;color:#e8ecf2;font-size:13px;box-sizing:border-box}
 button{margin-top:18px;width:100%;background:#2c6b4f;color:#fff;border:0;
        padding:10px;border-radius:6px;cursor:pointer;font-size:13px}
 .err{margin-top:14px;background:#5a2020;color:#f5b0b0;padding:9px 11px;
      border-radius:6px;font-size:12px}
 .note{color:#6f819b;font-size:11px;margin-top:16px;line-height:1.5}
</style></head><body>
<form class="box" method="post" action="/login">
  <h1>bank-cua console</h1>
  <div class="sub">MERIDIAN CORE &middot; sign in to continue</div>
  <label for="username">Sign-in</label>
  <input id="username" name="username" autofocus autocomplete="username"
         value="{{ username }}">
  <label for="password">Password</label>
  <input id="password" name="password" type="password"
         autocomplete="current-password">
  <button type="submit">Sign in</button>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <div class="note">Staff sign in with their Meridian operator id. Members sign
    in with their member number. What you can see and do is decided by the
    service after you sign in, not by this page.</div>
</form>
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
