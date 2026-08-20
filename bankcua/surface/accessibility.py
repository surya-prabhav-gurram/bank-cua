"""
A second Surface that perceives through an ACCESSIBILITY TREE and acts through
COORDINATES. It shares no targeting machinery with the Playwright web surface.

Why this exists
---------------
REPORT section 4 claims the `Surface` abstraction is the seam that lets this
system reach a legacy-web or desktop target without touching the schema, the
compiler, the replay engine, the error taxonomy or the safety model. Until a
second implementation existed, that was an argument. This is the demonstration.

What is real, and what is a stand-in
------------------------------------
Real: perception is a tree of {role, name, value, bounds} nodes and nothing else
-- no CSS, no XPath, no `element.fill()`, no DOM query of any kind. Action is a
mouse click at a point and a keystroke sequence. That is exactly the model a
Windows UIA or macOS AX driver works in, and it is why the artifact format had to
speak roles, names and proximity rather than selectors.

Stand-in: the tree is sourced from Chromium over CDP rather than from an OS
accessibility API, because this project has no desktop application to drive. The
delta to a real desktop driver is `_ax_nodes()` -- swap `Accessibility.getFullAXTree`
for `AXUIElementCopyAttributeValue` / `IUIAutomation::GetRootElement` and the rest
of this file is unchanged. That boundary is deliberately one method wide.

What building it proved
-----------------------
The legacy login form's inputs have NO accessible name -- no <label for>, no
aria-label -- so they are unaddressable by name on a DOM *and* on an a11y tree.
The only durable handle is the one a human uses: proximity to the words "User ID".
That finding is why `LocatorKind.NEAR_LABEL` exists: the web surface resolves it
structurally (same table row), this surface resolves it spatially (nearest control
to the right of, or below, the label's bounds). Same recorded intent, two honest
resolutions -- which is what makes an artifact portable across surfaces.
"""
from __future__ import annotations

from typing import Optional

from playwright.sync_api import sync_playwright

from ..schema import Checkpoint, ConditionDetector, Locator, LocatorKind
from ..replay.matching import text_match, url_match
from .base import ActResult, ElementInfo, Observation, ReadField, Surface

# Roles we treat as operable controls, in accessibility-tree terms.
_INTERACTIVE = {"button", "textbox", "link", "combobox", "checkbox", "radio",
                "searchbox", "menuitem", "tab", "switch", "spinbutton"}
_TEXT_ROLES = {"StaticText", "text", "heading", "paragraph", "cell", "LabelText",
               "columnheader", "rowheader", "gridcell"}
#: Grid CONTAINER roles. An accessibility tree emits a cell and, nested inside
#: it, the StaticText that renders its contents -- so every value appears twice
#: at nearly the same coordinates. Reading containers rather than their text
#: children is what a real AX grid reader does, and it also merges a multi-node
#: cell ("HOLD" + a "[HOLD]" badge) into the single value a person sees.
_CELL_ROLES = {"cell", "gridcell", "columnheader", "rowheader"}


class _AXNode:
    """One accessibility node: role, name, value, and where it is on screen."""

    __slots__ = ("frame", "h", "name", "role", "value", "w", "x", "y")

    def __init__(self, role, name, value, box, frame=""):
        self.role, self.name, self.value = role, name, value
        self.x, self.y, self.w, self.h = box
        self.frame = frame

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def __repr__(self) -> str:
        return f"<{self.role} {self.name!r} @({int(self.cx)},{int(self.cy)})>"


class AccessibilitySurface(Surface):
    """Perceive via the a11y tree; act via mouse and keyboard."""

    # No DOM: css / xpath / test_id are not resolvable here, and saying so lets
    # `portability_report` answer "will this artifact run on this surface?"
    # without launching anything.
    supported_locator_kinds = frozenset({
        LocatorKind.ROLE, LocatorKind.NEAR_LABEL, LocatorKind.LABEL,
        LocatorKind.PLACEHOLDER, LocatorKind.TEXT, LocatorKind.ALT_TEXT,
        LocatorKind.TITLE, LocatorKind.COORDINATES,
    })

    def __init__(self, base_url: str, headless: bool = True,
                 default_timeout_ms: int = 8000):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.default_timeout_ms = default_timeout_ms
        self._pw = None
        self._browser = None
        self._page = None
        self._cdp = None
        self._last_status: Optional[int] = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        ctx = self._browser.new_context()
        ctx.set_default_timeout(self.default_timeout_ms)
        self._page = ctx.new_page()
        self._page.on("response", self._on_response)
        self._cdp = ctx.new_cdp_session(self._page)

    def _on_response(self, response):
        try:
            if response.request.resource_type == "document" \
                    and response.frame == self._page.main_frame:
                self._last_status = response.status
        except Exception:
            pass

    def stop(self) -> None:
        for closer in (self._browser,):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    # ---- the ONE method a real desktop driver would replace ---------------
    def _ax_nodes(self) -> list[_AXNode]:
        """Snapshot the accessibility tree as flat {role, name, value, bounds}.

        A UIA/AX driver swaps the body of this method and nothing else: the
        vocabulary it returns is already the OS vocabulary.
        """
        out: list[_AXNode] = []
        for frame_id, frame_name in self._frames():
            try:
                args = {} if frame_id is None else {"frameId": frame_id}
                raw = self._cdp.send("Accessibility.getFullAXTree", args)["nodes"]
            except Exception:
                continue
            for n in raw:
                if n.get("ignored"):
                    continue
                role = (n.get("role") or {}).get("value") or ""
                if role not in _INTERACTIVE and role not in _TEXT_ROLES:
                    continue
                name = (n.get("name") or {}).get("value") or ""
                value = (n.get("value") or {}).get("value") or ""
                box = self._bounds(n.get("backendDOMNodeId"))
                if box is None:
                    continue
                out.append(_AXNode(role, name.strip(), str(value), box, frame_name))
        return out

    def _frames(self) -> list[tuple]:
        """(frameId, identifier) for the main document and every nested one.

        An accessibility tree stops at a frame boundary, so a legacy app built
        from framesets is invisible unless you walk them. The desktop analogue is
        identical: a UIA driver walks nested panes and child windows rather than
        assuming one flat tree.
        """
        try:
            tree = self._cdp.send("Page.getFrameTree")["frameTree"]
        except Exception:
            return [(None, "")]

        def walk(node, depth=0):
            f = node["frame"]
            ident = "" if depth == 0 else (f.get("name")
                                           or (f.get("url") or "").rsplit("/", 1)[-1])
            got = [(f["id"], ident)]
            for child in node.get("childFrames", []):
                got.extend(walk(child, depth + 1))
            return got

        return walk(tree)

    def _bounds(self, backend_id) -> Optional[tuple]:
        """Screen rectangle of a node. An OS driver reads AXPosition/AXSize."""
        if backend_id is None:
            return None
        try:
            quad = self._cdp.send("DOM.getBoxModel",
                                  {"backendNodeId": backend_id})["model"]["content"]
        except Exception:
            return None
        xs, ys = quad[0::2], quad[1::2]
        x, y = min(xs), min(ys)
        w, h = max(xs) - x, max(ys) - y
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)

    # ---- perception ------------------------------------------------------
    def index_elements(self) -> list[ElementInfo]:
        elems = []
        for i, n in enumerate(self._ax_nodes()):
            if n.role not in _INTERACTIVE:
                continue
            elems.append(ElementInfo(
                ref=i, role=n.role, name=n.name, tag=n.role,
                near_label=self._label_for(n), text=n.value,
                # No DOM: there is deliberately no css/xpath to offer.
                css="", xpath=""))
        return elems

    def index_readouts(self, start_ref: int) -> list[ReadField]:
        """Label/value pairs read spatially: a static text with a value to its
        right on the same line. The tabular reading a person does."""
        nodes = self._ax_nodes()
        texts = [n for n in nodes if n.role in _TEXT_ROLES and n.name]
        fields, ref = [], start_ref
        for label in texts:
            val = self._nearest_right(label, texts, exclude=label)
            if val is None or not val.name:
                continue
            fields.append(ReadField(
                ref=ref, label=label.name, value_preview=val.name[:60],
                locator=Locator(
                    description=f"value beside {label.name!r}",
                    candidates=[{"kind": LocatorKind.NEAR_LABEL,
                                 "value": label.name,
                                 "reasoning": "Spatially adjacent value cell."}])))
            ref += 1
        return fields

    def observe(self) -> Observation:
        nodes = self._ax_nodes()
        text = "\n".join(n.name for n in nodes if n.role in _TEXT_ROLES and n.name)
        return Observation(
            url=self.current_url(), title=self._page.title() or "",
            http_status=self._last_status, text_excerpt=text,
            elements=self.index_elements(),
            readouts=self.index_readouts(len(self.index_elements())))

    def locator_for_element(self, e: ElementInfo) -> Locator:
        cands = []
        if e.role and e.name:
            cands.append({"kind": LocatorKind.ROLE, "role": e.role, "value": e.name,
                          "reasoning": "Accessibility role + name."})
        if e.near_label:
            cands.append({"kind": LocatorKind.NEAR_LABEL, "value": e.near_label,
                          "role": e.role,
                          "reasoning": "No accessible name; addressed by the label "
                                       "beside it, resolved spatially."})
        if not cands:
            cands.append({"kind": LocatorKind.COORDINATES, "value": "0.5,0.5",
                          "reasoning": "Last resort: screen position."})
        return Locator(description=f"{e.role}: {e.name or e.near_label}"[:80],
                       candidates=cands)

    # ---- spatial reasoning (what replaces DOM structure) -----------------
    @staticmethod
    def _nearest_right(anchor: _AXNode, pool: list[_AXNode],
                       exclude=None) -> Optional[_AXNode]:
        """The node on the same line, to the right, that is closest."""
        best, best_dx = None, 1e9
        for n in pool:
            if n is exclude or n is anchor:
                continue
            same_line = abs(n.cy - anchor.cy) <= max(anchor.h, n.h)
            dx = n.x - (anchor.x + anchor.w)
            if same_line and dx >= -2 and dx < best_dx:
                best, best_dx = n, dx
        return best

    @staticmethod
    def _nearest_below(anchor: _AXNode, pool: list[_AXNode]) -> Optional[_AXNode]:
        best, best_dy = None, 1e9
        for n in pool:
            if n is anchor:
                continue
            aligned = abs(n.x - anchor.x) <= max(anchor.w, n.w)
            dy = n.y - (anchor.y + anchor.h)
            if aligned and 0 <= dy < best_dy:
                best, best_dy = n, dy
        return best

    def _label_for(self, control: _AXNode) -> str:
        """Reverse of the above: which static text labels this control?"""
        nodes = self._ax_nodes()
        texts = [n for n in nodes if n.role in _TEXT_ROLES and n.name]
        best, best_d = "", 1e9
        for t in texts:
            same_line = abs(t.cy - control.cy) <= max(t.h, control.h)
            dx = control.x - (t.x + t.w)
            if same_line and dx >= -2 and dx < best_d:
                best, best_d = t.name, dx
        return best

    # ---- resolution ------------------------------------------------------
    def _resolve(self, locator: Locator):
        """First honourable candidate that identifies exactly one node.

        Candidates this surface cannot honour (css/xpath/test_id) are SKIPPED,
        not failed: an artifact recorded on the web carries DOM strategies that
        are simply not meaningful here, and skipping them is how one recording
        serves both surfaces.
        """
        nodes = self._ax_nodes()
        if locator.frame_path:
            want = locator.frame_path[-1]
            scoped = [n for n in nodes if want in (n.frame or "")]
            nodes = scoped or nodes      # tolerant, like the web surface
        controls = [n for n in nodes if n.role in _INTERACTIVE]
        texts = [n for n in nodes if n.role in _TEXT_ROLES and n.name]
        for i, c in enumerate(locator.candidates):
            if c.kind not in self.supported_locator_kinds:
                continue
            hit = None
            if c.kind == LocatorKind.COORDINATES:
                return ("__coords__:" + c.value, i)
            if c.kind == LocatorKind.ROLE:
                hit = next((n for n in controls
                            if n.name == c.value
                            and (not c.role or n.role == c.role)), None)
            elif c.kind == LocatorKind.NEAR_LABEL:
                # The neighbour may be a control (a form field) or a static text
                # (a read-only value cell). Which one is decided by the caller's
                # role hint when it has one, and otherwise by pure adjacency --
                # exactly how a person reads a two-column legacy form.
                anchor = next((t for t in texts if t.name == c.value), None)
                if anchor is not None:
                    pool = [n for n in controls + texts if n is not anchor]
                    if c.role:
                        pool = [n for n in pool if n.role == c.role] or pool
                    hit = (self._nearest_right(anchor, pool)
                           or self._nearest_below(anchor, pool))
            else:
                # label / placeholder / text / alt / title all feed the SAME
                # accessible name on any a11y tree -- that is what the name IS.
                hit = next((n for n in controls + texts if n.name == c.value), None)
            if hit is not None:
                return (hit, i)
        return (None, None)

    # ---- action (mouse + keyboard only) ----------------------------------
    def navigate(self, url: str) -> ActResult:
        full = url if url.startswith("http") else self.base_url + (
            url if url.startswith("/") else "/" + url)
        try:
            resp = self._page.goto(full, wait_until="load")
            if resp is not None:
                self._last_status = resp.status
            return ActResult(ok=True, message=f"navigated to {full}")
        except Exception as ex:
            return ActResult(ok=False, message=f"navigate failed: {ex}")

    def _point_of(self, target):
        if isinstance(target, str):
            fx, fy = [float(v) for v in target.split(":", 1)[1].split(",")]
            vp = self._page.viewport_size or {"width": 1280, "height": 720}
            return fx * vp["width"], fy * vp["height"]
        return target.cx, target.cy

    def click(self, locator: Locator) -> ActResult:
        node, idx = self._resolve(locator)
        if node is None:
            return ActResult(ok=False,
                             message=f"no a11y node for {locator.description}")
        try:
            x, y = self._point_of(node)
            # A click may or may not navigate, and the difference is not knowable
            # in advance from a role and a name. Waiting on the CURRENT load state
            # is the classic trap: it is already "load", so the wait returns
            # instantly and the next step reads the old screen. Wait for the
            # navigation if one starts, and shrug if none does.
            try:
                with self._page.expect_navigation(wait_until="load", timeout=3000):
                    self._page.mouse.click(x, y)
            except Exception:
                pass
            return ActResult(ok=True, message=f"clicked {locator.description}",
                             candidate_index=idx)
        except Exception as ex:
            return ActResult(ok=False, message=f"click failed: {ex}",
                             candidate_index=idx)

    def fill(self, locator: Locator, text: str) -> ActResult:
        node, idx = self._resolve(locator)
        if node is None or isinstance(node, str):
            return ActResult(ok=False,
                             message=f"no a11y node for {locator.description}")
        try:
            self._page.mouse.click(node.cx, node.cy)
            self._page.keyboard.press("Control+A")
            self._page.keyboard.press("Delete")
            self._page.keyboard.type(text)
            return ActResult(ok=True, message=f"typed into {locator.description}",
                             candidate_index=idx)
        except Exception as ex:
            return ActResult(ok=False, message=f"fill failed: {ex}",
                             candidate_index=idx)

    def select_option(self, locator: Locator, value: str, by: str = "value") -> ActResult:
        """No <select> API here: a combobox is opened and driven by keyboard, the
        way a person does it."""
        node, idx = self._resolve(locator)
        if node is None or isinstance(node, str):
            return ActResult(ok=False, message=f"no a11y node for {locator.description}")
        try:
            # An accessibility tree exposes option LABELS, never the underlying
            # values -- and a desktop combobox has exactly the same property. So
            # a by-value selection recorded on the web is honoured here as a
            # by-label one, and the result says so rather than pretending.
            self._page.mouse.click(node.cx, node.cy)
            self._page.keyboard.type(value[:1])
            self._page.keyboard.press("Enter")
            note = "" if by == "label" else f" (recorded by={by}; resolved by label)"
            return ActResult(ok=True, message=f"selected {value}{note}",
                             candidate_index=idx)
        except Exception as ex:
            return ActResult(ok=False, message=f"select failed: {ex}",
                             candidate_index=idx)

    def press(self, key: str) -> ActResult:
        try:
            self._page.keyboard.press(key)
            return ActResult(ok=True, message=f"pressed {key}")
        except Exception as ex:
            return ActResult(ok=False, message=f"press failed: {ex}")

    def _read_table(self, anchor, idx) -> ActResult:
        """Reconstruct a grid from cell geometry rather than from markup.

        There is no <table> here to ask -- only nodes with roles and bounds. So
        rows are recovered the way a person recovers them: cells sharing a
        baseline are one row, and which COLUMN a cell belongs to is decided by
        where it sits horizontally relative to the header cells. This is the same
        reconstruction a UIA/AX driver performs on a desktop grid, which is why
        it is done this way rather than reaching for a DOM the surface lacks.

        Two things this gets right that counting cells does not, both found
        against the live target:
          * A status cell rendered as two nodes ("HOLD" plus a "[HOLD]" badge)
            makes its band WIDER than the header. Counting cells drops that row
            entirely -- silently losing the one member state anybody cares about.
            Position-mapping merges both nodes into the Status column.
          * The screen's footer function-key bar happens to have the same number
            of items as the grid has columns. Counting cells adopts it as data.
            A vertical-gap bound stops the read at the end of the grid instead.
        """
        below = [n for n in self._ax_nodes()
                 if n.role in _TEXT_ROLES and (n.name or "").strip()
                 and n.cy >= anchor.cy - 2]
        # Prefer containers; fall back to raw text only if this tree emits no
        # cell roles at all, which some applications genuinely do.
        cells = [n for n in below if n.role in _CELL_ROLES] or below
        if not cells:
            return ActResult(ok=False, candidate_index=idx,
                             message="no cell nodes at or below the anchor")

        bands: list[list] = []
        for n in sorted(cells, key=lambda c: (c.cy, c.x)):
            if bands and abs(n.cy - bands[-1][0].cy) <= max(n.h, bands[-1][0].h) / 2:
                bands[-1].append(n)
            else:
                bands.append([n])

        head_i, header = next(
            ((i, sorted(b, key=lambda c: c.x)) for i, b in enumerate(bands)
             if len(b) > 1), (None, None))
        if header is None:
            return ActResult(ok=False, candidate_index=idx,
                             message="no multi-column band to use as a header")
        names = [c.name.strip() or f"col{i}" for i, c in enumerate(header)]
        bounds = [c.x for c in header]

        def column_of(node) -> int:
            """Index of the header cell this node sits under."""
            best, best_d = 0, abs(node.x - bounds[0])
            for i, bx in enumerate(bounds):
                if abs(node.x - bx) < best_d:
                    best, best_d = i, abs(node.x - bx)
            return best

        row_h = max((c.h for c in header), default=12)
        rows, prev_cy = [], header[0].cy
        for band in bands[head_i + 1:]:
            cy = band[0].cy
            # A grid's rows are tightly stacked. A jump means the grid ended and
            # whatever follows -- actions, footer, key bar -- is a different
            # region that must not be read as data.
            if cy - prev_cy > row_h * 3:
                break
            cols: dict[int, list[str]] = {}
            for n in sorted(band, key=lambda c: c.x):
                cols.setdefault(column_of(n), []).append(n.name.strip())
            if len(cols) < 2:
                break
            rows.append({names[i]: " ".join(cols.get(i, [])) for i in range(len(names))})
            prev_cy = cy

        if not rows:
            return ActResult(ok=False, candidate_index=idx,
                             message="header found but no data rows beneath it")
        return ActResult(ok=True, rows=rows, value=str(len(rows)),
                         candidate_index=idx)

    def read(self, locator: Locator, attribute: str = "text") -> ActResult:
        node, idx = self._resolve(locator)
        if node is None or isinstance(node, str):
            return ActResult(ok=False, message=f"no a11y node for {locator.description}")
        if attribute == "table":
            return self._read_table(node, idx)
        # An a11y node carries both a name (what it is called) and a value (what
        # it holds). A read asking for "value" prefers the latter; anything else
        # is asking what the control says, which is the name. Either falls back to
        # the other, because legacy trees populate them inconsistently.
        text = ((node.value or node.name) if attribute == "value"
                else (node.name or node.value))
        return ActResult(ok=True, value=text.strip(), candidate_index=idx)

    # ---- verification ----------------------------------------------------
    def _all_text(self) -> str:
        return "\n".join(f"{n.name} {n.value}".strip() for n in self._ax_nodes())

    def check(self, cp: Checkpoint) -> bool:
        if cp.kind == "url_matches":
            return url_match(cp.value, self.current_url())
        if cp.kind == "http_status_lt":
            try:
                return (self._last_status or 0) < int(cp.value)
            except Exception:
                return False
        if cp.kind == "text_present":
            return text_match(cp.value, self._all_text())
        if cp.kind == "text_absent":
            return not text_match(cp.value, self._all_text())
        if cp.kind == "element_visible":
            return any(n.name == cp.value for n in self._ax_nodes())
        return False

    def detect(self, d: ConditionDetector) -> bool:
        if d.kind == "element_count_at_least":
            return sum(1 for n in self._ax_nodes()
                       if n.name.strip() == d.value) >= d.min_count
        if d.kind == "text_present":
            return text_match(d.value, self._all_text())
        if d.kind == "text_absent":
            return not text_match(d.value, self._all_text())
        if d.kind == "url_matches":
            return url_match(d.value, self.current_url())
        if d.kind == "http_status":
            return str(self._last_status) == str(d.value)
        if d.kind == "element_visible":
            return any(n.name == d.value for n in self._ax_nodes())
        return False

    # ---- evidence --------------------------------------------------------
    def screenshot(self, path: str) -> Optional[str]:
        try:
            self._page.screenshot(path=path, full_page=True)
            return path
        except Exception:
            return None

    def dom_snapshot(self) -> str:
        """There is no DOM on this surface. The equivalent debugging artefact is
        the tree we actually perceived, so that is what gets captured."""
        return "\n".join(
            f"{n.role}\tname={n.name!r}\tvalue={n.value!r}\t"
            f"@({int(n.x)},{int(n.y)},{int(n.w)}x{int(n.h)})"
            for n in self._ax_nodes())

    def current_url(self) -> str:
        return self._page.url if self._page else ""

    def wait_ms(self, ms: int) -> None:
        self._page.wait_for_timeout(ms)
