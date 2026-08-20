"""
A real co-browsing operator console over the existing CDP seam.

The handoff mechanism was already real -- pause, cede the control token, attach
to the SAME live Chromium, record what the human does, hand control back. What
was mocked was only the human's window onto it: a CLI where the operator typed
`--do click_selector:input[type=submit]`, which is not how a bank operator works.

This is that window. It serves the live page as a picture, forwards clicks at the
coordinates the human clicked on that picture, forwards keystrokes, and records
every one of them into the intervention before handing control back. No selectors
are involved anywhere, because the person is not thinking in selectors.

Threading note, which is the only non-obvious part: Playwright's sync API is not
safe to drive from multiple threads, and a web server is multi-threaded by
nature. So exactly one worker thread owns the browser connection and everything
else posts commands to it and waits. That is also how a production console would
be built -- the session is a single-owner resource, and pretending otherwise is
how you get impossible-to-debug interleaving on a live banking screen.
"""
from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Optional

from flask import Flask, Response, jsonify, request

from .handoff import HandoffStore, OperatorSession


class _SessionWorker:
    """Single owner of the live CDP session; everything else queues work to it."""

    def __init__(self, store: HandoffStore, request_id: str):
        self.store = store
        self.request_id = request_id
        self._q: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._session: Optional[OperatorSession] = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=20)

    def _run(self) -> None:
        try:
            self._session = OperatorSession(
                self.store.read(self.request_id), self.store).attach()
        except Exception as ex:                      # pragma: no cover - env dependent
            self._error = str(ex)
            self._ready.set()
            return
        self._ready.set()
        while True:
            fn, args, box = self._q.get()
            if fn is None:
                break
            try:
                box["value"] = fn(self._session, *args)
            except Exception as ex:
                box["error"] = str(ex)
            box["done"].set()

    def call(self, fn: Callable, *args, timeout: float = 20.0) -> Any:
        if self._error:
            raise RuntimeError(self._error)
        box: dict = {"done": threading.Event()}
        self._q.put((fn, args, box))
        if not box["done"].wait(timeout):
            raise TimeoutError("operator session did not respond")
        if "error" in box:
            raise RuntimeError(box["error"])
        return box.get("value")

    def close(self) -> None:
        try:
            self._q.put((None, (), {}))
        except Exception:
            pass


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Operator console — {rid}</title>
<style>
 body{{font:13px/1.5 -apple-system,Segoe UI,Arial;margin:0;
       background:#12161d;color:#e8ecf2}}
 header{{padding:10px 16px;background:#1f3a5f;display:flex;gap:16px;
         align-items:center}}
 header b{{font-size:15px}}
 .pill{{background:#2c6b4f;padding:2px 9px;border-radius:10px;font-size:11px}}
 .wrap{{display:flex;gap:16px;padding:16px}}
 .screen{{border:1px solid #33405a;border-radius:6px;overflow:hidden;
          background:#000}}
 .side{{width:320px}}
 .card{{background:#1a212c;border:1px solid #2a3444;border-radius:6px;
        padding:12px;margin-bottom:12px}}
 .card h3{{margin:0 0 6px;font-size:12px;text-transform:uppercase;
           color:#8fa3bf;letter-spacing:.05em}}
 button{{background:#2c6b4f;color:#fff;border:0;padding:8px 12px;
         border-radius:5px;cursor:pointer}}
 input{{width:100%;padding:7px;border-radius:5px;border:1px solid #33405a;
        background:#0e131a;color:#e8ecf2}}
 li{{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:#a9bcd6}}
 img{{display:block;cursor:crosshair}}
</style></head><body>
<header><b>Operator console</b><span class="pill" id="ctl">operator has control</span>
  <span id="rid">{rid}</span><span id="reason">{reason}</span></header>
<div class="wrap">
  <div class="screen"><img id="live" src="/screen?t=0" width="{w}"></div>
  <div class="side">
    <div class="card"><h3>Type</h3>
      <input id="text" placeholder="text, then Enter to send">
      <p><button onclick="key('Enter')">Enter</button>
         <button onclick="key('Tab')">Tab</button></p></div>
    <div class="card"><h3>Recorded actions</h3><ul id="log"></ul></div>
    <div class="card"><h3>Hand back</h3>
      <input id="note" placeholder="what you did / why">
      <p><button onclick="resolve(true)">Resume automation</button></p>
      <p><button onclick="resolve(false)">Abort run</button></p></div>
  </div>
</div>
<script>
let t=0;
function refresh(){{ t++; document.getElementById('live').src='/screen?t='+t; }}
setInterval(refresh, 900);
document.getElementById('live').addEventListener('click', e=>{{
  const r=e.target.getBoundingClientRect();
  post('/click', {{x:e.clientX-r.left, y:e.clientY-r.top}});
}});
document.getElementById('text').addEventListener('keydown', e=>{{
  if(e.key==='Enter'){{ post('/type', {{text:e.target.value}}); e.target.value=''; }}
}});
function key(k){{ post('/key', {{key:k}}); }}
function post(url, body){{
  fetch(url, {{method:'POST', headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify(body)}}).then(r=>r.json()).then(d=>{{ render(d.actions||[]); refresh(); }});
}}
function resolve(resume){{
  post('/resolve', {{resume:resume, note:document.getElementById('note').value}});
  document.getElementById('ctl').textContent = resume? 'control returned to agent' : 'run aborted';
}}
function render(a){{ document.getElementById('log').innerHTML =
  a.map(x=>'<li>'+x.op+' '+(x.detail||'')+'</li>').join(''); }}
fetch('/actions').then(r=>r.json()).then(d=>render(d.actions||[]));
</script></body></html>"""


def create_console(request_id: str, handoffs: str = "evidence/handoffs") -> Flask:
    app = Flask(__name__)
    store = HandoffStore(handoffs)
    worker = _SessionWorker(store, request_id)

    def _actions():
        return [a.model_dump() for a in store.read(request_id).human_actions]

    @app.get("/")
    def index():
        req = store.read(request_id)
        w, _h = worker.call(lambda s: s.viewport())
        return _PAGE.format(rid=req.id, reason=req.reason[:90], w=w)

    @app.get("/screen")
    def screen():
        png = worker.call(lambda s: s.screenshot_bytes())
        return Response(png, mimetype="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.get("/actions")
    def actions():
        return jsonify({"actions": _actions()})

    @app.post("/click")
    def click():
        b = request.get_json(force=True)
        worker.call(lambda s: s.do("click_xy", f"{b['x']},{b['y']}"))
        return jsonify({"actions": _actions()})

    @app.post("/type")
    def type_text():
        b = request.get_json(force=True)
        worker.call(lambda s: s.do("type", b.get("text", "")))
        return jsonify({"actions": _actions()})

    @app.post("/key")
    def key():
        b = request.get_json(force=True)
        worker.call(lambda s: s.do("key", b.get("key", "Enter")))
        return jsonify({"actions": _actions()})

    @app.post("/resolve")
    def resolve():
        b = request.get_json(force=True)
        note = b.get("note") or "resolved from the operator console"
        worker.call(lambda s: s.resolve(note=note, resume=bool(b.get("resume", True))))
        out = jsonify({"actions": _actions()})
        worker.close()
        return out

    return app
