"""Standalone configuration page (T7).

A separate process (``FI.py --config``) serving a small schema-driven web UI on
CONFIG_UI_PORT (default 8766). It is NOT the in-game runtime panel (webui.py) -
this is where the .env is edited visually.

Write endpoints are loopback-only (same policy as webui's config endpoints).
Setting changes go through ``config.set_runtime(..., persist=True)`` so a live
voice/web process picks them up via ``config.reload_if_changed()``.
"""

from __future__ import annotations

import json
import logging
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List
from urllib.parse import urlparse

import config_schema
from config import get_config

_log = logging.getLogger("f1_tr.config_ui")
DEFAULT_PORT = 8766


def _settings_payload() -> List[Dict[str, Any]]:
    cfg = get_config()
    out = []
    for s in config_schema.SCHEMA:
        value = cfg.get(s.key, str(s.default))
        if s.secret and value:
            value = ("*" * max(0, len(value) - 4)) + value[-4:]
        out.append({
            "key": s.key, "type": s.type, "default": s.default,
            "value": value, "group": s.group, "label": s.label,
            "help": s.help, "choices": list(s.choices),
            "min": s.min_value, "max": s.max_value,
            "restart_required": s.restart_required, "secret": s.secret,
        })
    return out


def _is_masked(value: Any) -> bool:
    """True for the '****abcd' form produced by _settings_payload()."""
    v = str(value or "")
    return len(v) > 4 and set(v[:-4]) == {"*"}


def apply_settings(updates: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + persist a batch of setting updates. Returns a result dict."""
    cfg = get_config()
    applied, errors = {}, {}
    for key, value in updates.items():
        s = config_schema.get_setting(key)
        if s is not None and s.secret and _is_masked(value):
            continue  # the page echoes the masked secret back; never persist it
        try:
            cfg.set_runtime(key, value, persist=True)
            applied[key] = cfg.get(key, "")
        except KeyError as e:
            errors[key] = f"not runtime: {e}"
        except ValueError as e:
            errors[key] = str(e)
    return {"applied": applied, "errors": errors,
            "persist_error": cfg.last_persist_error}


def _page() -> str:
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>F1 Race Engineer 设置</title><style>
body{font-family:system-ui,Segoe UI,sans-serif;max-width:820px;margin:20px auto;padding:0 16px}
h2{border-bottom:1px solid #ddd;padding-top:12px}
.row{display:flex;gap:10px;align-items:center;margin:6px 0}
label{flex:0 0 260px;font-size:14px}
input,select{flex:1;padding:4px 6px}
.hint{color:#888;font-size:12px}
button{padding:6px 14px;margin-top:12px}
.msg{margin-left:10px;color:#1a7f37}
.err{color:#c00}
</style></head><body>
<h1>F1 Race Engineer 设置</h1>
<p class="hint">保存后写入 .env；运行中的进程会自动热加载。</p>
<div id="form"></div>
<button onclick="save()">保存</button><span id="msg" class="msg"></span>
<script>
let schema=[];
async function load(){
  const r=await fetch('/api/schema'); const d=await r.json();
  schema=d.settings; const groups={};
  for(const s of schema){ (groups[s.group]=groups[s.group]||[]).push(s); }
  let html='';
  for(const g of Object.keys(groups)){
    html+=`<h2>${g}</h2>`;
    for(const s of groups[g]){
      const v=s.value==null?'':s.value;
      let input;
      if(s.type==='bool'){
        input=`<select id="f_${s.key}"><option value="1">开</option><option value="0">关</option></select>`;
      }else if(s.type==='enum'){
        input=`<select id="f_${s.key}">`+s.choices.map(c=>`<option>${c}</option>`).join('')+`</select>`;
      }else{
        input=`<input id="f_${s.key}" value="${v}">`;
      }
      html+=`<div class="row"><label>${s.label||s.key}<div class="hint">${s.key}${s.restart_required?' · 需重启':''}</div></label>${input}</div>`;
    }
  }
  document.getElementById('form').innerHTML=html;
  for(const s of schema){
    const el=document.getElementById('f_'+s.key); if(!el) continue;
    if(s.type==='bool') el.value=(String(s.value)==='1'||String(s.value).toLowerCase()==='true'||String(s.value).toLowerCase()==='on')?'1':'0';
    else el.value=s.value==null?'':s.value;
  }
}
async function save(){
  const updates={};
  for(const s of schema){ const el=document.getElementById('f_'+s.key); if(el) updates[s.key]=el.value; }
  const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(updates)});
  const d=await r.json();
  const m=document.getElementById('msg');
  if(d.errors && Object.keys(d.errors).length){ m.className='msg err'; m.textContent='部分失败: '+JSON.stringify(d.errors); }
  else { m.className='msg'; m.textContent='已保存'; }
}
load();
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "F1TRConfig/0.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _local(self) -> bool:
        addr = self.client_address[0] if self.client_address else ""
        return addr in ("127.0.0.1", "::1", "localhost")

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, _page().encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/api/schema":
            self._json(200, {"settings": _settings_payload(),
                             "port": get_config().get_int("CONFIG_UI_PORT", DEFAULT_PORT)})
        elif path == "/api/voices":
            self._json(200, {"voices": _voices()})
        elif path == "/api/settings":
            self._json(200, {"settings": _settings_payload()})
        else:
            self._json(404, {"error": "not found"})

    def _origin_ok(self) -> bool:
        # CSRF guard: browsers send Origin on cross-site POSTs (even no-cors).
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def do_POST(self):
        if not self._local() or not self._origin_ok():
            self._json(403, {"error": "localhost only"})
            return
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "invalid json"})
            return
        if path == "/api/settings":
            self._json(200, apply_settings(data))
        elif path == "/api/tts_preview":
            self._json(200, _tts_preview(data))
        elif path == "/api/llm_test":
            self._json(200, _llm_test())
        elif path == "/api/bind":
            self._json(200, {"binding": data.get("binding", "")})
        else:
            self._json(404, {"error": "not found"})


def _voices():
    try:
        from voices import list_voices
        return [{"id": v.id, "provider": v.provider, "voice": v.voice,
                 "label": v.label} for v in list_voices()]
    except Exception as e:  # noqa: BLE001
        return [{"error": str(e)}]


def _tts_preview(data):
    text = (data.get("text") or "测试播报").strip()
    try:
        from tts_client import make_tts
        eng = make_tts()
        if eng is None:
            return {"ok": False, "error": "无可用 TTS"}
        audio = eng.synthesize(text)
        import base64
        return {"ok": True, "mime": eng.mime,
                "audio_b64": base64.b64encode(audio).decode("ascii")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _llm_test():
    try:
        from llm_client import make_llm
        client = make_llm(get_config())
        if client is None or not client.configured:
            return {"ok": False, "error": "未配置 LLM key"}
        answer = client.chat([{"role": "user", "content": "ping"}], max_tokens=5)
        return {"ok": True, "answer": str(answer)[:60]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def serve(port: int = None, open_browser: bool = True,
          bind_ip: str = "127.0.0.1") -> None:
    cfg = get_config()
    port = port or cfg.get_int("CONFIG_UI_PORT", DEFAULT_PORT)
    httpd = ThreadingHTTPServer((bind_ip, port), _Handler)
    url = f"http://127.0.0.1:{port}"
    print(f">>> 设置页: {url} （Ctrl+C 退出）")
    if open_browser:
        threading.Timer(1.0, lambda: _safe_open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def _safe_open(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    serve()
