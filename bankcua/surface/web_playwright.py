"""
Concrete web Surface built on Playwright.

Notes for reviewers:
  * The browser is launched with a CDP endpoint (`--remote-debugging-port`) so a
    human operator can attach to the SAME live session during a handoff
    (see escalation/handoff.py). That is the load-bearing detail that makes
    control-transfer real rather than a fresh session.
  * Element indexing and accessible-name computation are done in-page via a small
    injected JS routine. Accessible-name is approximated (aria-label > labelled
    control > text/value > alt/title/placeholder); good enough for legacy
    surfaces and honest about what it does.
  * Frames are first-class: `frame_path` addresses controls inside <iframe>s
    (e.g. the savings-balance pane).
"""
from __future__ import annotations

import re
from typing import Optional

from playwright.sync_api import sync_playwright, Frame, Page, TimeoutError as PWTimeout

from ..schema import (
    Checkpoint,
    ConditionDetector,
    Locator,
    LocatorCandidate,
    LocatorKind,
)
from ..replay.matching import text_match, url_match
from .base import ActResult, ElementInfo, Observation, ReadField, Surface

# JS run inside each frame: enumerate interactive controls and describe them.
_INDEX_JS = r"""
() => {
  function visible(el){
    const s = window.getComputedStyle(el);
    if (s.display==='none' || s.visibility==='hidden' || s.opacity==='0') return false;
    const r = el.getBoundingClientRect();
    return r.width>0 && r.height>0;
  }
  function accName(el){
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    const lb = el.getAttribute('aria-labelledby');
    if (lb){ const t=document.getElementById(lb); if(t) return t.innerText.trim(); }
    if (el.tagName==='INPUT' && (el.type==='submit'||el.type==='button') && el.value)
      return el.value.trim();
    if (el.tagName==='A' || el.tagName==='BUTTON') return (el.innerText||'').trim();
    if (el.id){
      const l=document.querySelector('label[for="'+el.id+'"]');
      if(l) return l.innerText.trim();
    }
    if (el.alt) return el.alt.trim();
    if (el.title) return el.title.trim();
    if (el.placeholder) return el.placeholder.trim();
    return '';
  }
  function roleOf(el){
    if (el.getAttribute('role')) return el.getAttribute('role');
    const t = el.tagName.toLowerCase();
    if (t==='a' && el.hasAttribute('href')) return 'link';
    if (t==='button') return 'button';
    if (t==='select') return 'combobox';
    if (t==='textarea') return 'textbox';
    if (t==='input'){
      const ty=(el.type||'text').toLowerCase();
      if (ty==='submit'||ty==='button') return 'button';
      if (ty==='checkbox') return 'checkbox';
      if (ty==='radio') return 'radio';
      if (ty==='password'||ty==='text'||ty==='email'||ty==='search'||ty==='number'||ty==='tel')
        return 'textbox';
      return 'textbox';
    }
    return '';
  }
  function labelFor(el){
    if (el.id){
      const l=document.querySelector('label[for="'+el.id+'"]');
      if(l) return l.innerText.trim();
    }
    const p = el.closest('label'); if(p) return p.innerText.trim();
    return '';
  }
  function valueHint(el){
    // Progress feedback the model needs, WITHOUT leaking typed secrets.
    //
    // The selected option and checked state are safe to show outright. A text or
    // password value is not, and never is -- but withholding it entirely left
    // the model unable to see that its own fill had landed. A live discovery run
    // filled the same search box four times in a row and was stopped by the
    // stuck detector: from the observation alone, a filled box and an empty one
    // were indistinguishable.
    //
    // So text controls report EMPTINESS and nothing else: not the value, not its
    // length, not a prefix. That is enough to know a step is done and carries no
    // part of the secret.
    const t = el.tagName.toLowerCase();
    if (t==='select'){ const o=el.options[el.selectedIndex];
      return o ? ('selected: '+(o.text||'').trim()) : ''; }
    if (t==='input'){ const ty=(el.type||'').toLowerCase();
      if (ty==='checkbox'||ty==='radio') return el.checked?'checked':'unchecked';
      if (ty!=='submit' && ty!=='button' && ty!=='hidden')
        return el.value ? 'has a value' : 'empty'; }
    if (t==='textarea') return el.value ? 'has a value' : 'empty';
    return '';
  }
  function submitInfo(el){
    // Irreversibility signal: is this control a form submit, and does its form
    // use POST? Structural, not lexical -- it does not care what the button says.
    const t = el.tagName.toLowerCase();
    const ty = (el.getAttribute('type')||'').toLowerCase();
    const isSubmit = (t==='button' && (ty===''||ty==='submit')) ||
                     (t==='input' && ty==='submit');
    const form = el.closest('form');
    const method = form ? (form.getAttribute('method')||'get').toLowerCase() : '';
    return {is_submit: isSubmit, form_method: method};
  }
  function nearLabel(el){
    // legacy tables rarely use <label for>; infer a proximate label from the
    // enclosing table cell's preceding sibling, or the row's first cell.
    const cell = el.closest('td,th');
    if(cell){
      let p = cell.previousElementSibling;
      while(p){ if(p.tagName==='TD'||p.tagName==='TH'){ const t=(p.innerText||'').trim();
        if(t && t.length<=40) return t; } p=p.previousElementSibling; }
      const tr = cell.closest('tr');
      if(tr){ const first=tr.querySelector('td,th');
        if(first && first!==cell){ const t=(first.innerText||'').trim();
          if(t && t.length<=40) return t; } }
    }
    return '';
  }
  function cssPath(el){
    if (el.id) return el.tagName.toLowerCase()+'#'+CSS.escape(el.id);
    const parts=[];
    let cur=el, depth=0;
    while(cur && cur.nodeType===1 && depth<5){
      let sel=cur.tagName.toLowerCase();
      const par=cur.parentElement;
      if(par){
        const sibs=Array.from(par.children).filter(c=>c.tagName===cur.tagName);
        if(sibs.length>1) sel+=':nth-of-type('+(sibs.indexOf(cur)+1)+')';
      }
      parts.unshift(sel);
      cur=par; depth++;
    }
    return parts.join(' > ');
  }
  function xPath(el){
    const parts=[];
    let cur=el;
    while(cur && cur.nodeType===1){
      let ix=1, sib=cur.previousElementSibling;
      while(sib){ if(sib.tagName===cur.tagName) ix++; sib=sib.previousElementSibling; }
      parts.unshift(cur.tagName.toLowerCase()+'['+ix+']');
      cur=cur.parentElement;
    }
    return '/'+parts.join('/');
  }
  const sel = 'a[href], button, input, select, textarea, [role=button], [onclick]';
  const out=[];
  document.querySelectorAll(sel).forEach(el=>{
    if(el.type==='hidden') return;
    // SECURITY: never expose a control's typed value (el.value) in the
    // observation -- that would leak secrets/PII to the model and logs. Only
    // static text (innerText) is captured; submit-button labels come via accName.
    out.push({
      tag: el.tagName.toLowerCase(),
      role: roleOf(el),
      name: accName(el),
      type_attr: (el.getAttribute('type')||'').toLowerCase(),
      label: labelFor(el),
      placeholder: el.getAttribute('placeholder')||'',
      near_label: nearLabel(el),
      text: (valueHint(el) || (el.innerText||'').trim()).slice(0,80),
      is_submit: submitInfo(el).is_submit,
      form_method: submitInfo(el).form_method,
      css: cssPath(el),
      xpath: xPath(el),
      visible: visible(el),
      enabled: !el.disabled
    });
  });
  return out;
}
"""

# JS: enumerate 2-cell table rows as label/value read-only fields.
_READOUT_JS = r"""
() => {
  function cssPath(el){
    if (el.id) return el.tagName.toLowerCase()+'#'+CSS.escape(el.id);
    const parts=[]; let cur=el, depth=0;
    while(cur && cur.nodeType===1 && depth<6){
      let sel=cur.tagName.toLowerCase();
      const par=cur.parentElement;
      if(par){
        const sibs=Array.from(par.children).filter(c=>c.tagName===cur.tagName);
        if(sibs.length>1) sel+=':nth-of-type('+(sibs.indexOf(cur)+1)+')';
      }
      parts.unshift(sel); cur=par; depth++;
    }
    return parts.join(' > ');
  }
  const out=[];
  document.querySelectorAll('tr').forEach(tr=>{
    const tds=Array.from(tr.children).filter(c=>c.tagName==='TD');
    if(tds.length<2) return;
    // Pair each cell with the one IMMEDIATELY to its right, rather than pairing
    // the first cell with the last. Legacy detail screens routinely pack two
    // label/value pairs onto one row ("Member No. | 100234 | Name | Ada"), and
    // first-to-last silently reads every label against the WRONG value -- an
    // e-mail field returning a phone number, with nothing to indicate a fault.
    for(let i=0;i+1<tds.length;i+=2){
      const label=(tds[i].innerText||'').trim();
      const valCell=tds[i+1];
      const value=(valCell.innerText||'').trim();
      if(!label || !value || label===value) continue;
      if(label.length>40) continue;
      out.push({label:label, value:value, css:cssPath(valCell)});
    }
  });
  return out;
}
"""


# JS: read a resolved <table> into header-keyed rows.
_TABLE_JS = r"""
(el) => {
  const table = el.tagName === 'TABLE' ? el : el.closest('table');
  if (!table) return null;
  const rows = Array.from(table.rows).map(r =>
    Array.from(r.cells).map(c => (c.innerText || '').trim()));
  if (rows.length < 2) return null;
  // The header is the first row whose cells are all non-empty. Legacy grids
  // routinely open with a spacer or a title row spanning the width, and taking
  // row 0 unconditionally would key every field on ''.
  let h = 0;
  while (h < rows.length && rows[h].some(c => !c)) h++;
  if (h >= rows.length - 1) h = 0;
  const head = rows[h];
  return rows.slice(h + 1)
             .filter(r => r.length === head.length && r.some(c => c))
             .map(r => Object.fromEntries(head.map((k, i) => [k || ('col' + i), r[i]])));
}
"""


class WebSurface(Surface):
    # Everything except NEAR_LABEL's spatial form: on a DOM we resolve proximity
    # structurally, which is cheaper and exact.
    supported_locator_kinds = frozenset(LocatorKind)

    def __init__(self, base_url: str, headless: bool = True,
                 cdp_port: int = 0, default_timeout_ms: int = 8000):
        self.base_url = base_url.rstrip("/")
        self.headless = headless
        self.cdp_port = cdp_port
        self.default_timeout_ms = default_timeout_ms
        self._pw = None
        self._browser = None
        self._context = None
        self._page: Optional[Page] = None
        self._last_status: Optional[int] = None

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._pw = sync_playwright().start()
        args = []
        if self.cdp_port:
            args.append(f"--remote-debugging-port={self.cdp_port}")
        self._browser = self._pw.chromium.launch(headless=self.headless, args=args)
        self._context = self._browser.new_context()
        self._context.set_default_timeout(self.default_timeout_ms)
        self._page = self._context.new_page()
        self._page.on("response", self._on_response)

    def _on_response(self, response):
        try:
            req = response.request
            if req.resource_type == "document" and response.frame == self._page.main_frame:
                self._last_status = response.status
        except Exception:
            pass

    def stop(self) -> None:
        for closer in (self._context, self._browser):
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

    @property
    def cdp_endpoint(self) -> Optional[str]:
        return f"http://127.0.0.1:{self.cdp_port}" if self.cdp_port else None

    # ---- frame helpers ---------------------------------------------------
    def _frame_identifier(self, frame: Frame) -> str:
        if frame.name:
            return frame.name
        tail = re.sub(r"^https?://[^/]+", "", frame.url or "")
        return tail or frame.url

    def _frame_path_for(self, frame: Frame) -> list[str]:
        path, cur = [], frame
        main = self._page.main_frame
        while cur and cur != main:
            path.insert(0, self._frame_identifier(cur))
            cur = cur.parent_frame
        return path

    def _resolve_context(self, frame_path: list[str]):
        """Return the Frame/Page to resolve locators against."""
        if not frame_path:
            return self._page
        for fr in self._page.frames:
            if fr == self._page.main_frame:
                continue
            if self._frame_path_for(fr) == frame_path:
                return fr
        # tolerant fallback: match on the last identifier
        want = frame_path[-1]
        for fr in self._page.frames:
            if fr == self._page.main_frame:
                continue
            if fr.name == want or want in (fr.url or ""):
                return fr
        return None

    # ---- perception ------------------------------------------------------
    def index_elements(self) -> list[ElementInfo]:
        elems: list[ElementInfo] = []
        ref = 0
        for fr in self._page.frames:
            fp = self._frame_path_for(fr)
            try:
                descs = fr.evaluate(_INDEX_JS)
            except Exception:
                continue
            for d in descs:
                elems.append(ElementInfo(ref=ref, frame_path=fp, **d))
                ref += 1
        return elems

    def index_readouts(self, start_ref: int) -> list[ReadField]:
        fields: list[ReadField] = []
        ref = start_ref
        for fr in self._page.frames:
            fp = self._frame_path_for(fr)
            try:
                rows = fr.evaluate(_READOUT_JS)
            except Exception:
                continue
            for r in rows:
                cands = []
                if '"' not in r["label"]:
                    # Portable form FIRST, exactly as locator_for_element does for
                    # interactive controls. A read-only value has no accessible
                    # name of its own -- "$4,213.55" is not called anything -- so
                    # proximity to its label is the only durable handle, and it is
                    # the ONLY strategy a surface without a DOM can honour. Lead
                    # with the XPath instead and every extract step becomes
                    # unportable, which is a property of the recording, not of the
                    # flow.
                    cands.append(LocatorCandidate(
                        kind=LocatorKind.NEAR_LABEL, value=r["label"],
                        reasoning=f"Label-proximity: the value adjacent to "
                                  f"'{r['label']}'. A read-only value carries no "
                                  f"name of its own, so proximity is the durable "
                                  f"handle -- and stating it semantically lets a "
                                  f"non-DOM surface resolve it spatially."))
                    cands.append(LocatorCandidate(
                        kind=LocatorKind.XPATH,
                        value=f'//td[normalize-space(.)="{r["label"]}"]'
                              f'/following-sibling::td[1]',
                        reasoning="The same proximity bound to this DOM: the cell "
                                  f"immediately following '{r['label']}'; robust "
                                  "to table reflow, and correct on rows that pack "
                                  "more than one label/value pair."))
                cands.append(LocatorCandidate(
                    kind=LocatorKind.CSS, value=r["css"],
                    reasoning="Structural CSS of the value cell; fallback."))
                fields.append(ReadField(
                    ref=ref, frame_path=fp, label=r["label"],
                    value_preview=r["value"][:60],
                    locator=Locator(description=f"value for {r['label']}",
                                    frame_path=fp, candidates=cands)))
                ref += 1
        return fields

    def observe(self) -> Observation:
        page = self._page
        try:
            text = page.main_frame.evaluate(
                "() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            text = ""
        # append iframe text so the model/logs see sub-panes
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            try:
                t = fr.evaluate("() => document.body ? document.body.innerText : ''")
                if t:
                    text += f"\n[frame {self._frame_identifier(fr)}]\n{t}"
            except Exception:
                pass
        elems = self.index_elements()
        return Observation(
            url=page.url,
            title=(page.title() or ""),
            http_status=self._last_status,
            text_excerpt=text.strip(),
            elements=elems,
            readouts=self.index_readouts(len(elems)),
            frames=[self._frame_identifier(f) for f in page.frames
                    if f != page.main_frame],
        )

    # ---- locator synthesis ----------------------------------------------
    def locator_for_element(self, e: ElementInfo) -> Locator:
        """Build an ordered, most-stable-first candidate list from a live element."""
        cands: list[LocatorCandidate] = []
        if e.role and e.name:
            cands.append(LocatorCandidate(
                kind=LocatorKind.ROLE, role=e.role, value=e.name, exact=False,
                reasoning="Accessibility role + name; survives markup churn and "
                          "ports to a desktop accessibility tree."))
        if e.label:
            cands.append(LocatorCandidate(
                kind=LocatorKind.LABEL, value=e.label,
                reasoning="Associated <label> text; stable for form fields."))
        if e.placeholder:
            cands.append(LocatorCandidate(
                kind=LocatorKind.PLACEHOLDER, value=e.placeholder,
                reasoning="Placeholder text; stable-ish for inputs."))
        if e.role == "link" and e.text:
            cands.append(LocatorCandidate(
                kind=LocatorKind.TEXT, value=e.text,
                reasoning="Visible link text."))
        # Label-proximity: for legacy inputs with no <label for>, target the
        # control by the text of its adjacent cell. Far more stable across
        # theming/version skew than a structural path, and how a human reads the
        # form ("the box next to 'User ID'").
        if not e.name and not e.label and e.near_label and '"' not in e.near_label \
                and e.tag in ("input", "select", "textarea"):
            # Portable form FIRST: a statement of intent every surface can act on.
            cands.append(LocatorCandidate(
                kind=LocatorKind.NEAR_LABEL, value=e.near_label, role=e.role,
                reasoning=f"Label-proximity: the control adjacent to "
                          f"'{e.near_label}'. This control has no accessible name, "
                          f"so proximity is the only durable handle -- and stating "
                          f"it semantically lets a non-DOM surface resolve it "
                          f"spatially instead of structurally."))
            cands.append(LocatorCandidate(
                kind=LocatorKind.XPATH,
                value=f'//tr[td[normalize-space(.)="{e.near_label}"]]//{e.tag}',
                reasoning=f"The same proximity bound to this DOM: the {e.tag} in "
                          f"the row labelled '{e.near_label}'."))
        if e.css:
            cands.append(LocatorCandidate(
                kind=LocatorKind.CSS, value=e.css,
                reasoning="Structural CSS path; last-resort fallback, most "
                          "brittle to layout change."))
        if e.xpath:
            cands.append(LocatorCandidate(
                kind=LocatorKind.XPATH, value=e.xpath,
                reasoning="Absolute XPath; final fallback."))
        if not cands:  # guarantee >=1 candidate
            cands.append(LocatorCandidate(
                kind=LocatorKind.CSS, value=e.css or e.tag,
                reasoning="Tag fallback."))
        desc = (e.name or e.label or e.placeholder or e.text or e.role or e.tag)
        return Locator(description=f"{e.role or e.tag}: {desc}"[:80],
                       frame_path=e.frame_path, candidates=cands)

    def _pw_locator(self, ctx, c: LocatorCandidate):
        k = c.kind
        if k == LocatorKind.NEAR_LABEL:
            # "the thing adjacent to this label text", read structurally: the same
            # table row as a cell holding that text. Legacy layouts are
            # table-shaped, so the row IS the adjacency.
            #
            # Adjacency has two shapes, and a legacy form has both on the same
            # page: a WRITEABLE neighbour (the input beside "User ID") and a
            # READ-ONLY one (the value cell beside "Savings"). The a11y surface
            # already resolves either -- it searches controls and texts together
            # -- so resolving only the first here would make the same recorded
            # intent mean less on the surface it was recorded on, and every
            # extract would quietly resolve one candidate further down its list.
            if '"' in c.value:
                return None
            row = 'xpath=//tr[td[normalize-space(.)=%s]]' % f'"{c.value}"'
            control = ctx.locator(
                row + '//*[self::input or self::select or self::textarea]')
            try:
                if control.count() >= 1:
                    return control
            except Exception:
                return control
            # The cell immediately after the labelled one -- not the row's last
            # cell, which is a different cell entirely on a two-pair row.
            return ctx.locator(
                'xpath=//td[normalize-space(.)=%s]/following-sibling::td[1]'
                % f'"{c.value}"')
        if k == LocatorKind.ROLE:
            role = c.role or "button"
            if c.value:
                return ctx.get_by_role(role, name=c.value, exact=c.exact)
            return ctx.get_by_role(role)
        if k == LocatorKind.LABEL:
            return ctx.get_by_label(c.value, exact=c.exact)
        if k == LocatorKind.PLACEHOLDER:
            return ctx.get_by_placeholder(c.value, exact=c.exact)
        if k == LocatorKind.TEXT:
            return ctx.get_by_text(c.value, exact=c.exact)
        if k == LocatorKind.ALT_TEXT:
            return ctx.get_by_alt_text(c.value, exact=c.exact)
        if k == LocatorKind.TITLE:
            return ctx.get_by_title(c.value, exact=c.exact)
        if k == LocatorKind.TEST_ID:
            return ctx.get_by_test_id(c.value)
        if k == LocatorKind.CSS:
            return ctx.locator(c.value)
        if k == LocatorKind.XPATH:
            return ctx.locator("xpath=" + c.value)
        return None  # COORDINATES handled by caller

    def _resolve(self, locator: Locator):
        """Return (playwright_locator, candidate_index) for the first candidate
        that resolves to at least one element, else (None, None)."""
        ctx = self._resolve_context(locator.frame_path)
        if ctx is None:
            return None, None
        for i, c in enumerate(locator.candidates):
            if c.kind == LocatorKind.COORDINATES:
                return ("__coords__:" + c.value, i)
            try:
                loc = self._pw_locator(ctx, c)
                if loc is None:
                    continue
                if loc.count() >= 1:
                    return loc.first, i
            except Exception:
                continue
        return None, None

    # ---- actions ---------------------------------------------------------
    def navigate(self, url: str) -> ActResult:
        full = url if url.startswith("http") else self.base_url + (
            url if url.startswith("/") else "/" + url)
        try:
            resp = self._page.goto(full, wait_until="load")
            if resp is not None:
                self._last_status = resp.status
            return ActResult(ok=True, message=f"navigated to {full}",
                             value=str(self._last_status))
        except Exception as ex:
            return ActResult(ok=False, message=f"navigate failed: {ex}")

    def _coords_click(self, spec: str) -> ActResult:
        try:
            fx, fy = [float(v) for v in spec.split(":", 1)[1].split(",")]
            vp = self._page.viewport_size or {"width": 1280, "height": 720}
            self._page.mouse.click(fx * vp["width"], fy * vp["height"])
            return ActResult(ok=True, message="coordinate click")
        except Exception as ex:
            return ActResult(ok=False, message=f"coordinate click failed: {ex}")

    def click(self, locator: Locator) -> ActResult:
        loc, idx = self._resolve(locator)
        if loc is None:
            return ActResult(ok=False, message=f"could not resolve {locator.description}")
        if isinstance(loc, str):
            return self._coords_click(loc)
        try:
            loc.click(timeout=self.default_timeout_ms)
            return ActResult(ok=True, message=f"clicked {locator.description}",
                             candidate_index=idx)
        except Exception as ex:
            return ActResult(ok=False, message=f"click failed: {ex}", candidate_index=idx)

    def fill(self, locator: Locator, text: str) -> ActResult:
        loc, idx = self._resolve(locator)
        if loc is None or isinstance(loc, str):
            return ActResult(ok=False, message=f"could not resolve {locator.description}")
        try:
            loc.fill(text, timeout=self.default_timeout_ms)
            return ActResult(ok=True, message=f"filled {locator.description}",
                             candidate_index=idx)
        except Exception as ex:
            return ActResult(ok=False, message=f"fill failed: {ex}", candidate_index=idx)

    def select_option(self, locator: Locator, value: str, by: str = "value") -> ActResult:
        """Choose an option, resolving by whichever handle the control offers.

        A recorded step says WHICH OPTION was chosen; whether that string is the
        option's `value` or its visible label is a property of the markup, not of
        the intent. Legacy selects render "CODE - Long Description" as the label
        over a bare `CODE` value, so a recording that names the code and a
        control that matches on label disagree -- and Playwright answers that
        disagreement with an eight-second timeout rather than a useful error.

        Strategies are tried in order and the one that worked is REPORTED, so a
        recording that only succeeds via a fallback is visible rather than
        silently fine. Exact matches are tried before the prefix match, so
        "S0001" can never select "S0001-3" while an exact "S0001" option exists.
        """
        loc, idx = self._resolve(locator)
        if loc is None or isinstance(loc, str):
            return ActResult(ok=False, message=f"could not resolve {locator.description}")
        preferred = "label" if by == "label" else "value"
        alternate = "value" if preferred == "label" else "label"
        attempts = [(preferred, value), (alternate, value)]
        short = self.default_timeout_ms // 4 or 1000
        for kind, wanted in attempts:
            try:
                loc.select_option(**{kind: wanted}, timeout=short)
                note = "" if kind == preferred else f" (recorded by={by}; matched on {kind})"
                return ActResult(ok=True, message=f"selected {wanted}{note}",
                                 candidate_index=idx)
            except Exception:
                continue
        # Last resort: the label that STARTS WITH the recorded string. This is the
        # "CODE - Description" case and nothing else; a substring match anywhere
        # in the label would let a short code select an unrelated option.
        try:
            labels = loc.evaluate(
                "el => Array.from(el.options).map(o => o.text)")
            hit = next((t for t in labels
                        if t.strip().startswith(value.strip())), None)
            if hit is not None:
                loc.select_option(label=hit, timeout=short)
                return ActResult(ok=True, candidate_index=idx,
                                 message=f"selected {value} (matched the option "
                                         f"labelled {hit!r} by its leading code)")
            return ActResult(ok=False, candidate_index=idx,
                             message=f"select failed: no option matching {value!r}; "
                                     f"the control offers {labels}")
        except Exception as ex:
            return ActResult(ok=False, message=f"select failed: {ex}",
                             candidate_index=idx)

    def press(self, key: str) -> ActResult:
        try:
            self._page.keyboard.press(key)
            return ActResult(ok=True, message=f"pressed {key}")
        except Exception as ex:
            return ActResult(ok=False, message=f"press failed: {ex}")

    def read(self, locator: Locator, attribute: str = "text") -> ActResult:
        loc, idx = self._resolve(locator)
        if loc is None or isinstance(loc, str):
            return ActResult(ok=False, message=f"could not resolve {locator.description}")
        try:
            if attribute == "table":
                # Keyed on the grid\'s OWN headers rather than on positional
                # indices: a vendor that inserts a column between releases would
                # silently shift every positional read one place to the left,
                # and a balance read as a status is not a failure anyone sees.
                rows = loc.evaluate(_TABLE_JS)
                if not rows:
                    return ActResult(ok=False, candidate_index=idx,
                                     message=f"{locator.description} resolved but "
                                             f"held no readable table rows")
                return ActResult(ok=True, rows=rows, value=str(len(rows)),
                                 candidate_index=idx)
            if attribute in ("text", "inner_text"):
                val = loc.inner_text(timeout=self.default_timeout_ms)
            elif attribute == "value":
                # `.value` only exists on form controls; on a plain cell (a
                # common extraction target) fall back to its text so the read is
                # robust to the recorded attribute choice.
                try:
                    val = loc.input_value(timeout=self.default_timeout_ms)
                except Exception:
                    val = loc.inner_text(timeout=self.default_timeout_ms)
            elif attribute == "href":
                val = loc.get_attribute("href") or ""
            else:
                val = loc.inner_text(timeout=self.default_timeout_ms)
            return ActResult(ok=True, value=val.strip(), candidate_index=idx)
        except Exception as ex:
            return ActResult(ok=False, message=f"read failed: {ex}", candidate_index=idx)

    # ---- verification / detection ---------------------------------------
    def _frame_text(self, frame_path: list[str]) -> str:
        ctx = self._resolve_context(frame_path)
        if ctx is None:
            return ""
        try:
            if hasattr(ctx, "main_frame"):  # Page
                return ctx.main_frame.evaluate(
                    "() => document.body ? document.body.innerText : ''") or ""
            return ctx.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            return ""

    def _all_text(self) -> str:
        parts = []
        for fr in self._page.frames:
            try:
                parts.append(fr.evaluate(
                    "() => document.body ? document.body.innerText : ''") or "")
            except Exception:
                pass
        return "\n".join(parts)

    def check(self, cp: Checkpoint) -> bool:
        if cp.kind == "url_matches":
            return url_match(cp.value, self.current_url())
        if cp.kind == "http_status_lt":
            try:
                return (self._last_status or 0) < int(cp.value)
            except Exception:
                return False
        if cp.kind == "text_present":
            hay = self._frame_text(cp.frame_path) if cp.frame_path else self._all_text()
            return text_match(cp.value, hay)
        if cp.kind == "text_absent":
            hay = self._frame_text(cp.frame_path) if cp.frame_path else self._all_text()
            return not text_match(cp.value, hay)
        if cp.kind == "element_visible":
            ctx = self._resolve_context(cp.frame_path)
            if ctx is None:
                return False
            try:
                return ctx.locator(cp.value).first.is_visible(timeout=1500)
            except Exception:
                return False
        return False

    def detect(self, d: ConditionDetector) -> bool:
        if d.kind == "element_count_at_least":
            ctx = self._resolve_context(d.frame_path)
            if ctx is None:
                return False
            try:
                return ctx.get_by_text(d.value, exact=True).count() >= d.min_count
            except Exception:
                return False
        if d.kind == "text_present":
            hay = self._frame_text(d.frame_path) if d.frame_path else self._all_text()
            return text_match(d.value, hay)
        if d.kind == "text_absent":
            hay = self._frame_text(d.frame_path) if d.frame_path else self._all_text()
            return not text_match(d.value, hay)
        if d.kind == "url_matches":
            return url_match(d.value, self.current_url())
        if d.kind == "http_status":
            return str(self._last_status) == str(d.value)
        if d.kind == "element_visible":
            ctx = self._resolve_context(d.frame_path)
            if ctx is None:
                return False
            try:
                return ctx.locator(d.value).first.is_visible(timeout=1000)
            except Exception:
                return False
        return False

    # ---- evidence --------------------------------------------------------
    def screenshot(self, path: str) -> Optional[str]:
        try:
            self._page.screenshot(path=path, full_page=True)
            return path
        except Exception:
            return None

    def dom_snapshot(self) -> str:
        try:
            return self._page.content()
        except Exception:
            return ""

    def current_url(self) -> str:
        return self._page.url if self._page else ""

    def wait_ms(self, ms: int) -> None:
        self._page.wait_for_timeout(ms)

    def wait_for_selector(self, css: str, timeout_ms: int) -> bool:
        try:
            self._page.wait_for_selector(css, timeout=timeout_ms)
            return True
        except PWTimeout:
            return False
