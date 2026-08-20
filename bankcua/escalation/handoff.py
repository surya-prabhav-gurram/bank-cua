"""
Human-in-the-loop escalation and live-session control transfer.

The seam this implements (per the brief): automation must be able to *pause*,
*cede control*, and *resume on the same session*, with an explicit record of who
is in control.

Mechanism
---------
* Control token. Each intervention carries `controller` in {"agent","operator"}.
  While an intervention is open, the automation MUST NOT touch the page; the
  operator holds control. On resolution the token returns to "agent".

* Same live session. The browser is launched with a CDP endpoint
  (WebSurface(cdp_port=...)). The operator attaches to that SAME Chromium via
  `connect_over_cdp` and drives the SAME page. This is genuine co-control, not a
  new session -- cookies, in-progress form state, and the current URL are all
  preserved across the handoff.

* File-based inbox. Interventions are JSON files under a handoffs/ directory.
  raise_intervention() writes one and blocks (polling) for a resolution; a human
  operator (here, the `operator` CLI standing in for a console) resolves it. In
  unattended mode with no operator, the wait times out and the run aborts
  cleanly with the intervention preserved for later triage.

A full real-time co-browsing console is intentionally out of scope; the console
is mocked by the operator CLI, but the *handoff mechanism and control-transfer
model are real* (CDP attach to the live session, recorded human actions,
resume).
"""
from __future__ import annotations

import glob
import os
import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class InterventionKind(str, Enum):
    DISCOVERY_STUCK = "discovery_stuck"
    REPLAY_UNRECOVERABLE = "replay_unrecoverable"
    RISKY_CONFIRMATION = "risky_confirmation"
    DUAL_CONTROL = "dual_control"


class InterventionStatus(str, Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    ABORTED = "aborted"


class HumanAction(BaseModel):
    op: str                       # navigate | click_text | click_selector | fill
                                  # | click_xy | type | key | note
    detail: str = ""
    value: Optional[str] = None
    ts: float = 0.0


class InterventionRequest(BaseModel):
    """Everything a human needs to act, carried across the handoff."""
    id: str
    kind: InterventionKind
    reason: str
    capability_id: Optional[str] = None
    goal: Optional[str] = None
    current_step_index: Optional[int] = None
    state_url: str = ""
    screenshot_path: Optional[str] = None
    dom_path: Optional[str] = None
    cdp_endpoint: Optional[str] = Field(
        default=None,
        description="Attach here to take control of the SAME live session.",
    )
    controller: str = "operator"          # who holds control right now
    status: InterventionStatus = InterventionStatus.OPEN
    created_at: float = 0.0
    resolved_at: Optional[float] = None
    resolution_note: str = ""
    resume: bool = True                    # after resolution, should the run continue?
    human_actions: list[HumanAction] = Field(default_factory=list)


class HandoffStore:
    """File-based intervention inbox."""

    def __init__(self, root: str = "evidence/handoffs"):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, req_id: str) -> str:
        return os.path.join(self.root, f"{req_id}.json")

    def write(self, req: InterventionRequest) -> str:
        p = self._path(req.id)
        with open(p, "w") as f:
            f.write(req.model_dump_json(indent=2))
        return p

    def read(self, req_id: str) -> InterventionRequest:
        with open(self._path(req_id)) as f:
            return InterventionRequest.model_validate_json(f.read())

    def list_open(self) -> list[InterventionRequest]:
        out = []
        for p in sorted(glob.glob(os.path.join(self.root, "*.json"))):
            try:
                with open(p) as f:
                    r = InterventionRequest.model_validate_json(f.read())
                if r.status == InterventionStatus.OPEN:
                    out.append(r)
            except Exception:
                pass
        return out


class HandoffCoordinator:
    """Raises interventions and blocks the automation until they're resolved."""

    def __init__(self, store: HandoffStore, logger=None,
                 wait_timeout_s: float = 120.0):
        self.store = store
        self.logger = logger
        # How long automation holds the session open for a human. Two minutes
        # suits an unattended run, where the point is to fail promptly and leave
        # the request for triage. A person who has to be fetched, briefed and
        # walked to a screen needs longer, so it is a knob rather than a
        # constant -- and the run stays paused on a live session throughout,
        # which is the cost being traded against.
        self.wait_timeout_s = wait_timeout_s

    def raise_intervention(self, req: InterventionRequest) -> InterventionRequest:
        req.created_at = time.time()
        req.status = InterventionStatus.OPEN
        req.controller = "operator"        # cede control immediately
        self.store.write(req)
        if self.logger:
            self.logger.event("escalation_raised", id=req.id, kind=req.kind.value,
                              reason=req.reason, cdp_endpoint=req.cdp_endpoint,
                              step=req.current_step_index)
        return req

    def wait_for_resolution(self, req_id: str, timeout_s: Optional[float] = None,
                            poll_s: float = 1.0) -> InterventionRequest:
        """Block until an operator resolves/aborts, or time out (unattended).

        One case is decided without waiting at all. Taking control -- whether
        through the console or the `operator resolve` CLI -- requires attaching
        to the live session over CDP. If the run never exposed one, there is no
        path by which any operator can resolve this request, so blocking for the
        full timeout is a guaranteed dead wait: it delays the failure without
        changing it. Abort immediately, say why, and leave the request on disk
        for triage exactly as a timeout would.
        """
        first = self.store.read(req_id)
        # Only for a request still awaiting someone. One that has already been
        # resolved -- by an operator, or by a test, or out of band -- must be
        # returned as it stands, not overwritten.
        if first.status == InterventionStatus.OPEN and not first.cdp_endpoint:
            first.status = InterventionStatus.ABORTED
            first.resolution_note = (
                "no live session was exposed (the run was started without a CDP "
                "port), so no operator can take control of it")
            first.controller = "agent"
            self.store.write(first)
            if self.logger:
                self.logger.event("escalation_unattendable", id=req_id,
                                  reason=first.resolution_note)
            return first

        deadline = time.time() + (self.wait_timeout_s if timeout_s is None
                                  else timeout_s)
        while time.time() < deadline:
            cur = self.store.read(req_id)
            if cur.status != InterventionStatus.OPEN:
                if self.logger:
                    self.logger.event("escalation_resolved", id=req_id,
                                      status=cur.status.value,
                                      controller=cur.controller,
                                      human_actions=[a.model_dump() for a in cur.human_actions])
                return cur
            time.sleep(poll_s)
        # timed out with nobody home -> abort, keep the request for triage
        cur = self.store.read(req_id)
        cur.status = InterventionStatus.ABORTED
        waited = self.wait_timeout_s if timeout_s is None else timeout_s
        cur.resolution_note = (f"timed out after {waited:g}s waiting for a "
                               f"human reviewer")
        cur.controller = "agent"
        self.store.write(cur)
        if self.logger:
            self.logger.event("escalation_timeout", id=req_id)
        return cur


class OperatorSession:
    """Stand-in operator console: attaches to the SAME live session over CDP,
    performs manual actions, records them, and hands control back.

    This is what a real co-browsing console would do under the hood.
    """

    def __init__(self, req: InterventionRequest, store: HandoffStore):
        self.req = req
        self.store = store
        self._pw = None
        self._browser = None
        self.page = None

    def attach(self):
        from playwright.sync_api import sync_playwright
        if not self.req.cdp_endpoint:
            raise RuntimeError("no CDP endpoint on this intervention")
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.req.cdp_endpoint)
        ctx = self._browser.contexts[0]
        self.page = ctx.pages[0] if ctx.pages else ctx.new_page()
        self.req.controller = "operator"
        self.store.write(self.req)
        return self

    def _record(self, op: str, detail: str = "", value: Optional[str] = None):
        """Record the action AND persist it immediately.

        Deferring the write to resolve() would mean a console that crashes
        mid-handoff loses the record of what a human already did to a live
        banking screen -- the one part of this flow that cannot be reconstructed
        afterwards. Each action costs one small write; the audit trail survives.
        """
        self.req.human_actions.append(
            HumanAction(op=op, detail=detail, value=value, ts=time.time()))
        self.store.write(self.req)

    def do(self, op: str, detail: str = "", value: Optional[str] = None):
        """Perform + record a single manual action on the live page."""
        if op == "note":
            self._record("note", detail, value)
            return
        if op == "navigate":
            self.page.goto(detail)
        elif op == "click_text":
            self.page.get_by_text(detail).first.click()
        elif op == "click_selector":
            self.page.locator(detail).first.click()
        elif op == "fill":
            self.page.locator(detail).first.fill(value or "")
        elif op == "click_xy":
            # What a co-browsing console actually sends: a point on the picture
            # the human is looking at. No selector is involved, because the human
            # is not thinking in selectors.
            x, y = (float(v) for v in detail.split(","))
            self.page.mouse.click(x, y)
        elif op == "type":
            self.page.keyboard.type(detail)
        elif op == "key":
            self.page.keyboard.press(detail)
        else:
            raise ValueError(f"unknown operator op: {op}")
        self._record(op, detail, value)

    def screenshot_bytes(self) -> bytes:
        """The frame the operator is looking at. Polled rather than streamed:
        a screencast is a bandwidth optimisation, not a capability difference,
        and polling keeps the control-transfer model the thing on show."""
        return self.page.screenshot()

    def viewport(self) -> tuple:
        vp = self.page.viewport_size or {"width": 1280, "height": 720}
        return vp["width"], vp["height"]

    def resolve(self, note: str = "", resume: bool = True):
        self.req.status = InterventionStatus.RESOLVED
        self.req.resolved_at = time.time()
        self.req.resolution_note = note
        self.req.resume = resume
        self.req.controller = "agent"      # hand control back
        self.store.write(self.req)

    def detach(self):
        # detach WITHOUT closing the browser -- the automation still owns it
        try:
            if self._browser:
                self._browser.close()   # closes only this CDP connection
        finally:
            if self._pw:
                self._pw.stop()
