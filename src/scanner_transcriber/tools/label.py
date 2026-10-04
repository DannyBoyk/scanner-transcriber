"""Tiny local web page for correcting draft transcripts → <session>/refs.jsonl.

Stdlib only. Bind to a LAN address to use it from another machine; there is no auth,
so stop it when done.
"""
from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Label clips</title>
<style>
body{font:15px system-ui,sans-serif;max-width:60rem;margin:0 auto;padding:1rem;background:#fafafa;color:#222}
.clip{background:#fff;border:1px solid #ddd;border-radius:6px;padding:.6rem .8rem;margin:.6rem 0}
.clip.saved{border-color:#4a4}.meta{color:#666;font-size:13px}
textarea{width:100%;box-sizing:border-box;font:15px ui-monospace,monospace;min-height:3.2em}
.row{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}audio{height:32px}
.draft{color:#777;font-size:13px}
</style>
<h1>Label clips <small class=meta id=count></small></h1>
<p class=meta>Listen, then fix the text to what was actually said. Numbers can be digits
("1101", "10-4") or words; scoring treats them the same. Tab away or Ctrl+Enter saves.<br>
<b>No speech at all</b> (hum, tones, dead air): <b>clear the box</b> and save. That scores
hallucinations. <b>Speech you can't make out</b>: tick "skip".<br>
<b>Bad cut</b>: tick it if the clip starts late (first word cut off), ends early, or has two
separate transmissions in one clip, and add a few words in the note box.</p>
%ROWS%
<script>
async function save(id){
  const el=document.getElementById('c'+id);
  const body={id, text: el.querySelector('textarea').value.trim(),
              skip: el.querySelector('input.skip').checked,
              bad_cut: el.querySelector('input.badcut').checked,
              note: el.querySelector('input.note').value.trim()};
  const r=await fetch('/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(r.ok){el.classList.add('saved');count();}
}
function count(){document.getElementById('count').textContent=
  document.querySelectorAll('.clip.saved').length+' / '+document.querySelectorAll('.clip').length+' saved';}
document.querySelectorAll('.clip').forEach(el=>{
  const id=el.dataset.id, ta=el.querySelector('textarea');
  ta.addEventListener('change',()=>save(id));
  ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.ctrlKey){save(id);e.preventDefault();}});
  el.querySelectorAll('input').forEach(i=>i.addEventListener('change',()=>save(id)));
  el.querySelector('button').addEventListener('click',()=>save(id));
});
count();
</script>"""


def _load_jsonl(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    return {r["id"]: r for r in map(json.loads, path.open())}


def serve(session_dir: Path, host: str, port: int) -> None:
    meta = _load_jsonl(session_dir / "clips.jsonl")
    drafts = _load_jsonl(session_dir / "refs_draft.jsonl")
    refs_path = session_dir / "refs.jsonl"
    lock = threading.Lock()

    def render() -> bytes:
        refs = _load_jsonl(refs_path)
        rows = []
        for cid, d in drafts.items():
            m = meta.get(cid, {})
            ref = refs.get(cid)
            text = ref["text"] if ref else d["draft"]
            rows.append(
                f'<div class="clip{" saved" if ref else ""}" id="c{cid}" data-id="{cid}">'
                f'<div class=row><b>{cid}</b><audio controls preload=none src="/clip/{cid}.wav"></audio>'
                f'<span class=meta>{m.get("duration_ms", 0) / 1000:.1f}s · TG {html.escape(str(m.get("tgid")))} '
                f'{html.escape(str(m.get("department")))} / {html.escape(str(m.get("channel")))}</span></div>'
                f'<textarea>{html.escape(text)}</textarea>'
                f'<div class=row><span class=draft>draft: {html.escape(d["draft"])}</span>'
                + (f'<span class=draft>· second opinion: {html.escape(d["alt"])}</span>' if d.get("alt") else "")
                + f'<label><input class=skip type=checkbox{" checked" if ref and ref.get("skip") else ""}> skip</label>'
                f'<label><input class=badcut type=checkbox{" checked" if ref and ref.get("bad_cut") else ""}> bad cut</label>'
                f'<input class=note placeholder="note" size=24 value="{html.escape((ref or {}).get("note", ""))}">'
                f'<button>save</button></div></div>')
        return PAGE.replace("%ROWS%", "\n".join(rows)).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                return self._send(200, render(), "text/html; charset=utf-8")
            if self.path.startswith("/clip/") and self.path.endswith(".wav"):
                cid = self.path[len("/clip/"):-4]
                if cid in drafts and cid.isalnum():
                    return self._send(200, (session_dir / "clips" / f"{cid}.wav").read_bytes(),
                                      "audio/wav")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if self.path != "/save":
                return self._send(404, b"", "text/plain")
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if body.get("id") not in drafts:
                return self._send(400, b"unknown id", "text/plain")
            with lock:
                refs = _load_jsonl(refs_path)
                refs[body["id"]] = {"id": body["id"], "text": str(body.get("text", "")),
                                    "skip": bool(body.get("skip")),
                                    "bad_cut": bool(body.get("bad_cut")),
                                    "note": str(body.get("note", ""))[:500]}
                tmp = refs_path.with_suffix(".tmp")
                tmp.write_text("".join(json.dumps(r) + "\n" for r in refs.values()))
                tmp.replace(refs_path)
            self._send(200, b"ok", "text/plain")

    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"labelling {len(drafts)} clips at http://{host}:{port}/  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
