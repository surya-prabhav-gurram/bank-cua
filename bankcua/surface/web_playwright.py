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
    // Progress feedback the model needs, WITHOUT leaking typed secrets: expose
    // the selected option / checked state, but never a text/password value.
    const t = el.tagName.toLowerCase();
    if (t==='select'){ const o=el.options[el.selectedIndex];
      return o ? ('selected: '+(o.text||'').trim()) : ''; }
    if (t==='input'){ const ty=(el.type||'').toLowerCase();
      if (ty==='checkbox'||ty==='radio') return el.checked?'checked':'unchecked'; }
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
    const label=(tds[0].innerText||'').trim();
    const valCell=tds[tds.length-1];
    const value=(valCell.innerText||'').trim();
    if(!label || !value || label===value) return;
    if(label.length>40) return;
    out.push({label:label, value:value, css:cssPath(valCell)});
  });
  return out;
}
"""


class WebSurface(Surface):
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
                    cands.append(LocatorCandidate(
                        kind=LocatorKind.XPATH,
                        value=f'//tr[td[normalize-space(.)="{r["label"]}"]]'
                              f'/td[last()]',
                        reasoning="Label-relative XPath: finds the value cell by "
                                  "its adjacent label; robust to table reflow."))
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
            cands.append(LocatorCandidate(
                kind=LocatorKind.XPATH,
                value=f'//tr[td[normalize-space(.)="{e.near_label}"]]//{e.tag}',
                reasoning=f"Label-proximity: the {e.tag} in the row labelled "
                          f"'{e.near_label}'. Survives markup/theming churn far "
                          f"better than an absolute path."))
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
        loc, idx = self._resolve(locator)
        if loc is None or isinstance(loc, str):
            return ActResult(ok=False, message=f"could not resolve {locator.description}")
        try:
            if by == "label":
                loc.select_option(label=value, timeout=self.default_timeout_ms)
            else:
                loc.select_option(value=value, timeout=self.default_timeout_ms)
            return ActResult(ok=True, message=f"selected {value}", candidate_index=idx)
        except Exception as ex:
            return ActResult(ok=False, message=f"select failed: {ex}", candidate_index=idx)

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
            return cp.value in self.current_url()
        if cp.kind == "http_status_lt":
            try:
                return (self._last_status or 0) < int(cp.value)
            except Exception:
                return False
        if cp.kind == "text_present":
            hay = self._frame_text(cp.frame_path) if cp.frame_path else self._all_text()
            return cp.value in hay
        if cp.kind == "text_absent":
            hay = self._frame_text(cp.frame_path) if cp.frame_path else self._all_text()
            return cp.value not in hay
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
        if d.kind == "text_present":
            hay = self._frame_text(d.frame_path) if d.frame_path else self._all_text()
            return d.value in hay
        if d.kind == "url_matches":
            return d.value in self.current_url()
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
