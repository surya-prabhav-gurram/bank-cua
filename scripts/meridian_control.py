#!/usr/bin/env python3
"""
Put MERIDIAN CORE into a known fault state before a scenario runs.

Why this exists as a separate tool, outside the automation
----------------------------------------------------------
The target is a SHARED demo host with a global fault-injection setting that any
visitor can change. During development it was found at `errorRate=1.0` with a
forced `validation` inject -- every request failing, set by someone else. A test
suite that assumes a clean target on that host does not test anything; it just
reports whatever the last stranger left behind.

So scenarios declare the fault state they need, the same way round-1 scenarios
armed the local mock's `_control` plane. What changed is only that the control
plane now belongs to somebody else's server.

The split this preserves is the point
-------------------------------------
`config/policy.meridian.yaml` BLOCKS `*/settings*` for the agent. A recorded
capability must never be able to reach the screen that governs whether faults
occur -- an automation that can switch off its own error conditions can hide its
own failures, and a green run would stop meaning anything.

The harness may set up the world. The automation may not change the conditions
it is being judged under. This file is the harness half of that split, and it
deliberately speaks HTTP directly rather than going through Surface/replay, so
it cannot be mistaken for -- or reused as -- a capability.

Usage:
    python scripts/meridian_control.py show
    python scripts/meridian_control.py reset              # 0.0, no forced inject
    python scripts/meridian_control.py set --rate 0.2
    python scripts/meridian_control.py set --inject maintenance
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "https://web-sample.interface-hiring.com"
# Public demo credentials, published in the assignment brief. Real operator
# secrets never appear in this repo -- see bankcua/safety/credentials.py.
SETUP_OPERATOR = ("teller1", "password", "MAIN-001")

INJECT_KINDS = ("", "validation", "notfound", "permission", "timeout",
                "maintenance", "server")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Return redirects instead of following them.

    Sign-on answers 302 and sets the session cookie on THAT response. When the
    redirect is followed and its target happens to be faulting -- which is the
    normal state of this host while a forced inject is set -- the error unwinds
    the request and the cookie is lost, leaving this tool unable to authenticate
    precisely when the fault it exists to clear is switched on.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


class TargetControl:
    """A tiny authenticated HTTP client for the target's own settings screen."""

    def __init__(self, base: str = BASE):
        self.base = base.rstrip("/")
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            _NoRedirect())

    def _open(self, path: str, body: bytes | None = None) -> str:
        """Read the response body whatever the status.

        This tool exists BECAUSE the target is faulting, so it cannot treat a
        non-2xx as fatal: a forced `validation` inject makes sign-on itself
        answer 400, and raising there would leave the only tool that can clear
        the fault unable to run while the fault is set -- locked out by exactly
        the condition it is for.
        """
        try:
            with self._opener.open(self.base + path, body, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            return ex.read().decode("utf-8", "replace")

    def _post(self, path: str, data: dict) -> str:
        return self._open(path, urllib.parse.urlencode(data).encode())

    def _get(self, path: str) -> str:
        return self._open(path)

    def signon(self) -> "TargetControl":
        op, pw, branch = SETUP_OPERATOR
        self._post("/signon", {"operator": op, "password": pw, "branch": branch})
        return self

    def read(self, tries: int = 4) -> dict:
        """Read the settings screen, retrying past faults on the way in.

        The settings screen is exempt from the target's own fault injection --
        otherwise a forced `timeout` would lock everyone, including this tool,
        out of the only page that can clear it. Sign-on is NOT exempt, so getting
        here can still fail transiently; retry rather than report a state we
        never actually read.
        """
        html = ""
        for _ in range(tries):
            html = self._get("/settings")
            if "SYSTEM SETTINGS" in html:
                break
            self.signon()
        rate = re.search(r'name="errorRate"[^>]*value="([^"]*)"', html)
        inject = re.search(r'<option value="([a-z]*)" selected>', html)
        token = re.search(r'name="_token" value="([^"]*)"', html)
        if not rate:
            raise RuntimeError(
                "could not read the target's settings screen; it may be "
                "unreachable or the markup changed")
        return {"error_rate": rate.group(1) if rate else "?",
                "forced_inject": inject.group(1) if inject else "",
                "_token": token.group(1) if token else ""}

    def apply(self, error_rate: str, forced_inject: str) -> dict:
        if forced_inject not in INJECT_KINDS:
            raise ValueError(f"unknown inject kind {forced_inject!r}; "
                             f"expected one of {INJECT_KINDS}")
        # The token is read back from the live page rather than cached: the
        # target rotates it per session, and a stale one is exactly the failure
        # this tool would otherwise introduce into every scenario it sets up.
        self._post("/settings", {"_token": self.read()["_token"],
                                 "errorRate": error_rate,
                                 "forcedInject": forced_inject})
        return self.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="meridian_control")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="print the target's current fault state")
    sub.add_parser("reset", help="0.0 error rate, no forced inject")
    st = sub.add_parser("set", help="set a specific fault state")
    st.add_argument("--rate", default="0.0")
    st.add_argument("--inject", default="", choices=INJECT_KINDS)
    args = ap.parse_args(argv)

    tc = TargetControl().signon()
    if args.cmd == "show":
        print(tc.read())
        return 0
    rate, inject = ("0.0", "") if args.cmd == "reset" else (args.rate, args.inject)
    before = tc.read()
    after = tc.apply(rate, inject)
    print(f"before: rate={before['error_rate']} inject={before['forced_inject']!r}")
    print(f"after : rate={after['error_rate']} inject={after['forced_inject']!r}")
    # The target is shared: if someone else writes between our POST and our
    # read-back, say so rather than reporting a state we did not actually set.
    # Compared numerically because the app normalises "0.0" to "0" on echo, and
    # a warning that fires on formatting is a warning people learn to ignore.
    def _same_rate(a: str, b: str) -> bool:
        try:
            return abs(float(a) - float(b)) < 1e-9
        except ValueError:
            return a == b

    if not _same_rate(after["error_rate"], rate) or after["forced_inject"] != inject:
        print("WARNING: target did not settle on the requested state "
              "(another client may be writing to the same host)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
