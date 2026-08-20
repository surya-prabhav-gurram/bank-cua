"""
Mock legacy bank back-office application (proxy target for the CUA system).

This intentionally imitates the realities described in the brief:
  * Server-rendered HTML, table-based layout, NON-semantic markup, NO test IDs.
  * A subsection (the savings-balance pane) rendered inside an <iframe>, so any
    automation must be frame-aware (a common legacy pain point).
  * Cookie-based session with a short TTL that can expire mid-flow.
  * A multi-step flow: login -> search -> member detail -> open sub-account ->
    review -> confirmation.

It also exposes *deterministic, injectable* error/exception states so replay
error-handling can be exercised without flakiness:

  Special member IDs (business outcomes):
    00000  -> "no member found"        (BUSINESS: MEMBER_NOT_FOUND)
    99999  -> member exists but locked (BUSINESS: PERMISSION_DENIED; the denial
              screen still shows the member NAME but withholds the balance)

  Injection via /_control/set (test-only, documented) OR ?inject= query param:
    interstitial -> an unexpected "System Maintenance Notice" gate page
    timeout      -> force the session to be expired (redirect to login)
    swallow      -> the member-search input accepts a fill then discards it
                    (legacy input-masking); the keystroke succeeds and the page
                    looks normal, so only Step.verify_value catches it
    slow         -> add a multi-second delay before responding
    error500     -> return an HTTP 500 application error page

The app holds no real data and no real credentials.
Login: username 'operator', password 'password123' (fake, for the demo only).
"""
from __future__ import annotations

import os
import time

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    make_response,
    Response,
)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Tenant variants: the SAME underlying vendor product ("Corebank") shipped to
# different institutions with different branding, labels, and control captions.
# This is exactly the multi-tenant reality the artifact must generalise across:
# the flow and page structure are identical; the visible strings differ.
# Select with MOCKBANK_VARIANT=corebank|summit.
# ---------------------------------------------------------------------------
VARIANTS = {
    "corebank": {
        "brand": "Corebank&nbsp;Servicing&nbsp;Console", "header_bg": "#1f3a5f",
        "user_label": "User ID", "signon": "Sign On",
        "mid_label": "Member ID", "search": "Search",
        "footer": "Corebank v7.2.1 (legacy) &mdash; internal use only",
    },
    "summit": {
        "brand": "Summit&nbsp;Credit&nbsp;Union&nbsp;&mdash;&nbsp;Member&nbsp;Services",
        "header_bg": "#2c5f3a",
        "user_label": "Username", "signon": "Log In",
        "mid_label": "Member Number", "search": "Find",
        "footer": "Summit CU Console v3.4 (powered by Corebank) &mdash; internal use only",
    },
}
VARIANT = VARIANTS.get(os.environ.get("MOCKBANK_VARIANT", "corebank"),
                       VARIANTS["corebank"])

# ---------------------------------------------------------------------------
# Fake data (no real PII).  balance stored in cents to avoid float drift.
# ---------------------------------------------------------------------------
MEMBERS = {
    "12345": {"name": "Jane A. Doe", "savings_cents": 421355, "status": "active"},
    "22222": {"name": "John Q. Public", "savings_cents": 1000000, "status": "active"},
    "54321": {"name": "Maria Gonzalez", "savings_cents": 87542, "status": "active"},
    "99999": {"name": "Restricted Account", "savings_cents": 0, "status": "locked"},
}

VALID_USER = ("operator", "password123")
SESSION_TTL_SECONDS = 15 * 60  # 15 minutes

# Test-only injection state. Settable via /_control/set?key=<k>&value=<v>.
# 'armed' one-shot flags fire on the next matching page then clear themselves.
_CONTROL: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Tiny HTML helpers -- deliberately table-based, no ids / test-ids / classes.
# ---------------------------------------------------------------------------
def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title></head>
<body bgcolor="#f4f4f0">
<table width="760" cellpadding="6" cellspacing="0" border="0" align="center"
       bgcolor="#ffffff">
 <tr bgcolor="{VARIANT['header_bg']}"><td>
   <font face="Arial" color="#ffffff" size="4"><b>{VARIANT['brand']}</b></font>
 </td></tr>
 <tr><td>{body}</td></tr>
 <tr bgcolor="#dddddd"><td>
   <font face="Arial" size="1">{VARIANT['footer']}</font>
 </td></tr>
</table>
</body></html>"""


def _fmt_money(cents: int) -> str:
    return "${:,.2f}".format(cents / 100.0)


def _session_ok() -> bool:
    raw = request.cookies.get("cbsession")
    if not raw:
        return False
    if _CONTROL.get("timeout") == "on":
        return False
    try:
        issued = float(raw.split(":", 1)[1])
    except Exception:
        return False
    return (time.time() - issued) <= SESSION_TTL_SECONDS


def _maybe_inject() -> Response | None:
    """Return an injected response (interstitial/slow/500) if armed, else None.

    Injection can be armed globally via /_control/set or per-request via ?inject=.
    'interstitial' is one-shot: it fires once then disarms so the following
    (post-handoff) request proceeds normally.
    """
    what = request.args.get("inject") or _CONTROL.get("inject")
    if what == "slow":
        time.sleep(3.2)
        return None
    if what == "error500":
        body = ("<font face='Arial' color='#b00020'><b>Application Error</b></font>"
                "<p><font face='Arial'>An unexpected error occurred while "
                "processing your request (ref 500-CB). Please try again.</font></p>")
        return Response(_page("Error", body), status=500)
    if what == "interstitial":
        # one-shot: clear the armed flag so resume proceeds
        if _CONTROL.get("inject") == "interstitial":
            _CONTROL.pop("inject", None)
        nxt = request.full_path.rstrip("?")
        # strip our own inject param from the continue target
        cont = nxt.replace("inject=interstitial", "").replace("&&", "&").rstrip("?&")
        body = f"""
        <font face="Arial" size="3" color="#8a6d00"><b>System Maintenance Notice</b></font>
        <p><font face="Arial">A scheduled maintenance window is in effect.
        Some services may be briefly unavailable.</font></p>
        <table cellpadding="4"><tr><td>
          <a href="{cont or url_for('home')}">
            <font face="Arial"><b>Continue to application &raquo;</b></font></a>
        </td></tr></table>"""
        return Response(_page("Maintenance Notice", body))
    return None


# ---------------------------------------------------------------------------
# Test-only control plane (documented; would not exist in a real app)
# ---------------------------------------------------------------------------
@app.route("/_control/set")
def control_set():
    k = request.args.get("key", "")
    v = request.args.get("value", "")
    if v == "":
        _CONTROL.pop(k, None)
    else:
        _CONTROL[k] = v
    return {"ok": True, "control": _CONTROL}


@app.route("/_control/reset")
def control_reset():
    _CONTROL.clear()
    return {"ok": True}


@app.route("/healthz")
def healthz():
    return {"ok": True}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
@app.route("/")
def root():
    return redirect(url_for("home") if _session_ok() else url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    err = ""
    if request.method == "POST":
        u = request.form.get("username", "")
        p = request.form.get("password", "")
        if (u, p) == VALID_USER:
            resp = make_response(redirect(url_for("home")))
            resp.set_cookie("cbsession", f"{u}:{time.time()}", httponly=True)
            return resp
        err = ("<tr><td colspan='2'><font face='Arial' color='#b00020'>"
               "Invalid username or password.</font></td></tr>")
    body = f"""
    <font face="Arial" size="3"><b>Operator Sign On</b></font>
    <form method="POST" action="/login">
    <table cellpadding="4" cellspacing="0" border="0">
      {err}
      <tr><td><font face="Arial">{VARIANT['user_label']}</font></td>
          <td><input type="text" name="username" size="24"></td></tr>
      <tr><td><font face="Arial">Password</font></td>
          <td><input type="password" name="password" size="24"></td></tr>
      <tr><td></td><td><input type="submit" value="{VARIANT['signon']}"></td></tr>
    </table>
    </form>"""
    return _page("Sign On", body)


@app.route("/logout")
def logout():
    resp = make_response(redirect(url_for("login")))
    resp.delete_cookie("cbsession")
    return resp


def _require_session() -> Response | None:
    if not _session_ok():
        body = ("<font face='Arial' color='#b00020'><b>Session expired</b></font>"
                "<p><font face='Arial'>Your session has timed out. "
                "<a href='/login'>Sign on again</a>.</font></p>")
        return Response(_page("Session expired", body), status=440)
    return None


# ---------------------------------------------------------------------------
# Member search / detail
# ---------------------------------------------------------------------------
@app.route("/home")
def home():
    inj = _maybe_inject()
    if inj:
        return inj
    guard = _require_session()
    if guard:
        return guard
    # `swallow` injection: a legacy input-masking handler that accepts the write
    # and then discards it. Chosen deliberately over `readonly`, which the
    # automation layer already refuses to type into: this one SUCCEEDS at the
    # keystroke level and leaves the page looking entirely normal, so neither the
    # action result nor a page-state checkpoint can see it. That is precisely the
    # blind spot Step.verify_value exists to close.
    swallow = (' oninput="this.value=\'\'"'
               if _CONTROL.get("inject") == "swallow" else "")
    body = f"""
    <font face="Arial" size="3"><b>Member Search</b></font>
    <form method="GET" action="/member">
    <table cellpadding="4" cellspacing="0" border="0"><tr>
      <td><font face="Arial">{VARIANT['mid_label']}</font></td>
      <td><input type="text" name="mid" size="18"{swallow}></td>
      <td><input type="submit" value="{VARIANT['search']}"></td>
    </tr></table>
    </form>
    <p><font face="Arial" size="2">Tip: enter a member number to view the
    account summary.</font></p>"""
    return _page("Member Search", body)


@app.route("/member")
def member():
    inj = _maybe_inject()
    if inj:
        return inj
    guard = _require_session()
    if guard:
        return guard

    mid = (request.args.get("mid") or "").strip()
    if mid == "":
        body = """
        <font face="Arial" color="#b00020"><b>Validation error:</b>
        Member ID is required.</font>
        <p><a href="/home"><font face="Arial">Back to search</font></a></p>"""
        return Response(_page("Validation error", body), status=400)

    rec = MEMBERS.get(mid)
    if rec is None:
        body = f"""
        <font face="Arial" size="3"><b>Search Result</b></font>
        <p><font face="Arial" color="#b00020">No member found for ID
        '{mid}'.</font></p>
        <p><a href="/home"><font face="Arial">Back to search</font></a></p>"""
        return _page("No member found", body)

    if rec["status"] == "locked":
        # A real servicing console still says WHOSE account was refused, so the
        # operator can route the request -- it withholds the balance, not the
        # identity. That asymmetry is what KnownCondition.surfaces_outputs models:
        # a legitimate non-success that still carries some declared data.
        body = f"""
        <font face="Arial" color="#b00020" size="3"><b>Access Denied</b></font>
        <table cellpadding="4" cellspacing="0" border="0" width="100%">
          <tr><td width="120"><font face="Arial">Member ID</font></td>
              <td><font face="Arial"><b>{mid}</b></font></td></tr>
          <tr><td><font face="Arial">Name</font></td>
              <td><font face="Arial">{rec['name']}</font></td></tr>
        </table>
        <p><font face="Arial">You do not have permission to view this member's
        accounts. This account is restricted.</font></p>"""
        return Response(_page("Access denied", body), status=403)

    # Normal detail page. Balance lives in an IFRAME (legacy sub-pane).
    body = f"""
    <font face="Arial" size="3"><b>Member Detail</b></font>
    <table cellpadding="4" cellspacing="0" border="0" width="100%">
      <tr><td width="120"><font face="Arial">Member ID</font></td>
          <td><font face="Arial"><b>{mid}</b></font></td></tr>
      <tr><td><font face="Arial">Name</font></td>
          <td><font face="Arial">{rec['name']}</font></td></tr>
    </table>
    <p><font face="Arial" size="2"><b>Accounts</b></font></p>
    <iframe name="balancepane" src="/member/balancepane?mid={mid}"
            width="480" height="90" frameborder="1"></iframe>
    <p>
      <a href="/subaccount/new?mid={mid}">
        <font face="Arial"><b>Open New Sub-Account</b></font></a>
    </p>"""
    return _page("Member Detail", body)


@app.route("/member/balancepane")
def balancepane():
    # Rendered inside the iframe -- no page chrome.
    guard = _require_session()
    if guard:
        return guard
    mid = (request.args.get("mid") or "").strip()
    rec = MEMBERS.get(mid)
    if rec is None:
        return "<font face='Arial'>n/a</font>"
    bal = _fmt_money(rec["savings_cents"])
    return f"""<!DOCTYPE html><html><body bgcolor="#ffffff">
    <table cellpadding="3" cellspacing="0" border="0">
      <tr bgcolor="#e8e8e8">
        <td><font face="Arial" size="2"><b>Account</b></font></td>
        <td><font face="Arial" size="2"><b>Balance</b></font></td></tr>
      <tr><td><font face="Arial" size="2">Savings</font></td>
          <td><font face="Arial" size="2">{bal}</font></td></tr>
      <tr><td><font face="Arial" size="2">Checking</font></td>
          <td><font face="Arial" size="2">$0.00</font></td></tr>
    </table></body></html>"""


# ---------------------------------------------------------------------------
# Sub-account creation flow (multi-step, with an irreversible final action)
# ---------------------------------------------------------------------------
@app.route("/subaccount/new")
def subaccount_new():
    inj = _maybe_inject()
    if inj:
        return inj
    guard = _require_session()
    if guard:
        return guard
    mid = (request.args.get("mid") or "").strip()
    if MEMBERS.get(mid) is None:
        return redirect(url_for("member", mid=mid))
    body = f"""
    <font face="Arial" size="3"><b>Open Sub-Account</b> &mdash; member {mid}</font>
    <form method="POST" action="/subaccount/review">
    <input type="hidden" name="mid" value="{mid}">
    <table cellpadding="4" cellspacing="0" border="0">
      <tr><td><font face="Arial">Account Type</font></td>
          <td><select name="acct_type">
                <option value="">-- select --</option>
                <option value="SAV">Savings</option>
                <option value="MMK">Money Market</option>
                <option value="CD">Certificate of Deposit</option>
              </select></td></tr>
      <tr><td><font face="Arial">Initial Deposit</font></td>
          <td><input type="text" name="deposit" size="12"></td></tr>
      <tr><td></td><td><input type="submit" value="Review"></td></tr>
    </table>
    </form>"""
    return _page("Open Sub-Account", body)


@app.route("/subaccount/review", methods=["POST"])
def subaccount_review():
    guard = _require_session()
    if guard:
        return guard
    mid = request.form.get("mid", "")
    acct_type = request.form.get("acct_type", "")
    deposit = request.form.get("deposit", "")
    if acct_type == "" or deposit.strip() == "":
        body = """
        <font face="Arial" color="#b00020"><b>Validation error:</b>
        Account type and initial deposit are required.</font>
        <p><a href="/home"><font face="Arial">Back</font></a></p>"""
        return Response(_page("Validation error", body), status=400)
    label = {"SAV": "Savings", "MMK": "Money Market", "CD": "Certificate of Deposit"}.get(
        acct_type, acct_type)
    body = f"""
    <font face="Arial" size="3"><b>Review Sub-Account</b></font>
    <p><font face="Arial">Please confirm the details below. This action will
    create a new sub-account and <b>cannot be undone</b>.</font></p>
    <table cellpadding="4" cellspacing="0" border="1" bordercolor="#cccccc">
      <tr><td><font face="Arial">Member</font></td>
          <td><font face="Arial">{mid}</font></td></tr>
      <tr><td><font face="Arial">Account Type</font></td>
          <td><font face="Arial">{label}</font></td></tr>
      <tr><td><font face="Arial">Initial Deposit</font></td>
          <td><font face="Arial">{deposit}</font></td></tr>
    </table>
    <form method="POST" action="/subaccount/confirm">
      <input type="hidden" name="mid" value="{mid}">
      <input type="hidden" name="acct_type" value="{acct_type}">
      <input type="hidden" name="deposit" value="{deposit}">
      <input type="submit" value="Confirm and Create">
    </form>"""
    return _page("Review Sub-Account", body)


# deterministic sub-account numbering so replay assertions are stable
_SUBACCT_SEQ = {"n": 8100}


@app.route("/subaccount/confirm", methods=["POST"])
def subaccount_confirm():
    guard = _require_session()
    if guard:
        return guard
    mid = request.form.get("mid", "")
    _SUBACCT_SEQ["n"] += 1
    new_no = f"SA-{mid}-{_SUBACCT_SEQ['n']}"
    body = f"""
    <font face="Arial" size="3" color="#0a6b1e"><b>Sub-Account Created</b></font>
    <p><font face="Arial">The new sub-account has been created successfully.</font></p>
    <table cellpadding="4" cellspacing="0" border="0">
      <tr><td><font face="Arial">Confirmation</font></td>
          <td><font face="Arial"><b>{new_no}</b></font></td></tr>
      <tr><td><font face="Arial">Member</font></td>
          <td><font face="Arial">{mid}</font></td></tr>
    </table>
    <p><a href="/home"><font face="Arial">Return to search</font></a></p>"""
    return _page("Sub-Account Created", body)


if __name__ == "__main__":
    port = int(os.environ.get("MOCKBANK_PORT", "5057"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
