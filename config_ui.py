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
            "help": s.help, "label_en": s.label_en, "help_en": s.help_en,
            "choices": list(s.choices), "choices_from": s.choices_from,
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


def _groups_payload() -> Dict[str, Dict[str, str]]:
    return {k: {"zh": v[0], "en": v[1]}
            for k, v in config_schema.GROUP_LABELS.items()}


def _audio_payload() -> Dict[str, Any]:
    """Device lists for the mic/speaker pickers (empty when audio is absent)."""
    try:
        import audio
        return {"input": audio.list_devices("input"),
                "output": audio.list_devices("output"),
                "current": audio.current(),
                "active": audio.describe()["active"]}
    except Exception as e:  # noqa: BLE001 - a broken audio stack must not kill the page
        return {"input": [], "output": [], "current": {}, "active": {},
                "error": str(e)}


def _page() -> str:
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>F1 Race Engineer 设置</title><style>
body{font-family:system-ui,Segoe UI,sans-serif;max-width:860px;margin:20px auto;padding:0 16px}
h2{border-bottom:1px solid #ddd;padding-top:14px}
.row{display:flex;gap:10px;align-items:center;margin:8px 0}
label{flex:0 0 280px;font-size:14px}
.hint{color:#888;font-size:12px;margin-top:2px}
input,select{flex:1;padding:5px 8px}
button{padding:6px 14px;margin-top:12px}
.msg{margin-left:10px;color:#1a7f37}
.err{color:#c00}
.lang{float:right;font-size:13px}
.lang a{color:#06c;text-decoration:none;margin-left:8px}
#tip{position:fixed;display:none;pointer-events:none;z-index:9999;max-width:380px;
     background:#0b1218;color:#e6edf3;border:1px solid #2a3542;padding:7px 10px;
     border-radius:8px;font-size:12px;line-height:1.55;box-shadow:0 4px 16px rgba(0,0,0,.5)}
[data-help]{cursor:help}
</style></head><body>
<div id="tip"></div>
<h1>F1 Race Engineer 设置 <span class="lang">
  <a href="#" id="zh">中文</a><a href="#" id="en">English</a></span></h1>
<p class="hint" id="tipLine">保存后写入 .env；运行中的进程会自动热加载。</p>
<div id="form"></div>
<button id="saveBtn" onclick="save()">保存</button><span id="msg" class="msg"></span>
<script>
let schema=[], groups={}, voices=null;
let LANG = localStorage.getItem('f1tr_lang') || 'zh';
const T = {
  zh:{tip:'保存后写入 .env；运行中的进程会自动热加载。', save:'保存', saved:'已保存',
      partial:'部分失败: ', restart:' · 需重启', on:'开', off:'关',
      auto:'（自动）', bind:'⌨ 按键绑定', bindWait:'请按一下要绑定的键…',
      bindOk:'✓ 已识别：', bindNone:'没检测到按键（超时）',
      follow:'跟随系统当前设备', rescan:'🔄 重新检测', missing:'（未检测到）',
      sysdefault:'系统默认'},
  en:{tip:'Saved to .env; the running app hot-reloads it.', save:'Save', saved:'Saved',
      partial:'Some failed: ', restart:' · restart', on:'On', off:'Off',
      auto:'(auto)', bind:'⌨ Bind key', bindWait:'Press the key to bind…',
      bindOk:'✓ Detected: ', bindNone:'No key detected (timeout)',
      follow:'Follow system default', rescan:'🔄 Re-scan', missing:' (not found)',
      sysdefault:'system default'}
};
function t(k){ return (T[LANG]||T.zh)[k]; }
function pick(obj, base){ return LANG==='en' ? (obj[base+'_en']||obj[base]) : (obj[base]||obj[base+'_en']); }
async function load(){
  const r=await fetch('/api/schema'); const d=await r.json();
  schema=d.settings; groups=d.groups||{};
  const byGroup={};
  for(const s of schema){ (byGroup[s.group]=byGroup[s.group]||[]).push(s); }
  let html='';
  for(const g of Object.keys(byGroup)){
    const gl = groups[g] ? groups[g][LANG] : g;
    html+=`<h2>${gl}</h2>`;
    for(const s of byGroup[g]){
      const id='f_'+s.key;
      let input;
      if(s.choices_from==='voices'){
        input=`<select id="${id}"><option value="">${t('auto')}</option></select>`;
      }else if(s.type==='bool'){
        input=`<select id="${id}"><option value="1">${t('on')}</option><option value="0">${t('off')}</option></select>`;
      }else if(s.type==='enum'){
        input=`<select id="${id}">`+s.choices.map(c=>`<option>${c}</option>`).join('')+`</select>`;
      }else if(s.key==='AUDIO_INPUT'||s.key==='AUDIO_OUTPUT'){
        input=`<select id="${id}" style="flex:1"><option value="">${t('follow')}</option></select>`
            +`<button type="button" class="audBtn" style="margin-top:0;margin-left:6px;">${t('rescan')}</button>`;
      }else{
        input=`<input id="${id}" value="${s.value==null?'':s.value}">`;
      }
      const bind = (s.key==='PTT_BINDING') ? `<button type="button" class="bindBtn" data-for="${id}" style="margin-top:0;margin-left:6px;">${t('bind')}</button>` : '';
      html+=`<div class="row" data-help-holder="${id}"><label>${pick(s,'label')||s.key}`
          +`<div class="hint">${s.key}${s.restart_required?t('restart'):''}</div></label>`
          +`<span style="flex:1;display:flex;align-items:center;">${input}${bind}</span></div>`;
    }
  }
  document.getElementById('form').innerHTML=html;
  for(const s of schema){
    const el=document.getElementById('f_'+s.key);
    if(!el) continue;
    if(s.type==='bool'){ const v=String(s.value).toLowerCase();
      el.value=(v==='1'||v==='true'||v==='on')?'1':'0'; }
    else if(s.choices_from==='voices'){ await fillVoices(el, s.value); }
    else if(s.key==='AUDIO_INPUT'||s.key==='AUDIO_OUTPUT'){ /* filled by loadAudio() */ }
    else el.value=s.value==null?'':s.value;
  }
  // tooltips: hover anywhere on a row
  for(const s of schema){
    const holder=document.querySelector(`[data-help-holder="f_${s.key}"]`);
    const help=pick(s,'help');
    if(holder && help) holder.setAttribute('data-help', help);
  }
  document.querySelectorAll('.bindBtn').forEach(b=>{ b.onclick=()=>startBind(b.getAttribute('data-for')); });
  document.querySelectorAll('.audBtn').forEach(b=>{ b.onclick=async()=>{ await loadAudio(); }; });
  await loadAudio();
  document.getElementById('tipLine').textContent=t('tip');
  document.getElementById('saveBtn').textContent=t('save');
}
async function fillVoices(sel, cur){
  if(!voices){ try{ const r=await fetch('/api/voices'); voices=(await r.json()).voices||[]; }catch(e){ voices=[]; } }
  for(const v of voices){ const o=document.createElement('option'); o.value=v.id; o.textContent=v.label||v.id; sel.appendChild(o); }
  if(cur) sel.value=cur;
}
async function loadAudio(){
  let d;
  try{ const r=await fetch('/api/audio'); d=await r.json(); }catch(e){ return; }
  const fill=(sel, list, cur)=>{
    if(!sel) return;
    sel.innerHTML=`<option value="">${t('follow')}</option>`;
    let found=false;
    for(const dev of (list||[])){ const o=document.createElement('option'); o.value=dev.name;
      o.textContent=dev.name+(dev.default?(' ('+t('sysdefault')+')'):'');
      if(cur&&dev.name===cur){ o.selected=true; found=true; } sel.appendChild(o); }
    // Unplugged pin stays visible/kept, never silently shown as "follow".
    if(cur&&!found){ const o=document.createElement('option'); o.value=cur;
      o.textContent=cur+t('missing'); o.selected=true; sel.appendChild(o); }
  };
  fill(document.getElementById('f_AUDIO_INPUT'), d.input, (d.current||{}).input);
  fill(document.getElementById('f_AUDIO_OUTPUT'), d.output, (d.current||{}).output);
}
async function startBind(inputId){
  const m=document.getElementById('msg'); m.className='msg'; m.textContent=t('bindWait');
  try{
    const r=await fetch('/api/bind',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({timeout_s:8})});
    const d=await r.json();
    if(d.binding){ const el=document.getElementById(inputId); if(el) el.value=d.binding;
      m.textContent=t('bindOk')+d.binding; }
    else { m.className='msg err'; m.textContent=t('bindNone'); }
  }catch(e){ m.className='msg err'; m.textContent=t('bindNone'); }
}
async function save(){
  const updates={};
  for(const s of schema){ const el=document.getElementById('f_'+s.key); if(el) updates[s.key]=el.value; }
  const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(updates)});
  const d=await r.json();
  const m=document.getElementById('msg');
  if(d.errors && Object.keys(d.errors).length){ m.className='msg err'; m.textContent=t('partial')+JSON.stringify(d.errors); }
  else { m.className='msg'; m.textContent=t('saved'); }
}
function setLang(l){ LANG=l; localStorage.setItem('f1tr_lang', l); voices=null; load(); }
document.getElementById('zh').onclick=(e)=>{e.preventDefault();setLang('zh');};
document.getElementById('en').onclick=(e)=>{e.preventDefault();setLang('en');};
// hover tooltip (same behaviour as the runtime panel)
(function(){
  const tip=document.getElementById('tip');
  document.addEventListener('mouseover',(e)=>{
    const el=e.target.closest && e.target.closest('[data-help]');
    if(!el || !el.getAttribute('data-help')){ tip.style.display='none'; return; }
    tip.textContent=el.getAttribute('data-help'); tip.style.display='block'; move(e);
  });
  document.addEventListener('mouseout',(e)=>{ const el=e.target.closest && e.target.closest('[data-help]'); if(el) tip.style.display='none'; });
  function move(e){
    if(tip.style.display!=='block') return;
    const pad=14; let x=e.clientX+pad, y=e.clientY+pad; const r=tip.getBoundingClientRect();
    if(x+r.width>window.innerWidth-8) x=e.clientX-r.width-pad;
    if(y+r.height>window.innerHeight-8) y=e.clientY-r.height-pad;
    tip.style.left=Math.max(4,x)+'px'; tip.style.top=Math.max(4,y)+'px';
  }
  document.addEventListener('mousemove',move);
  window.addEventListener('scroll',()=>{tip.style.display='none';},true);
})();
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
                             "groups": _groups_payload(),
                             "port": get_config().get_int("CONFIG_UI_PORT", DEFAULT_PORT)})
        elif path == "/api/voices":
            self._json(200, {"voices": _voices()})
        elif path == "/api/audio":
            self._json(200, _audio_payload())
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
        # Malformed Content-Length must not escape as an unhandled ValueError
        # (ThreadingHTTPServer would drop the connection with no response), and
        # an oversized body is rejected before it is read into memory.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > 256 * 1024:
            self._json(413, {"error": "body too large"})
            return
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
            timeout = data.get("timeout_s", 8) if isinstance(data, dict) else 8
            try:
                timeout = max(1.0, min(30.0, float(timeout)))
            except (TypeError, ValueError):
                timeout = 8.0
            from input_sources import capture_keyboard_binding
            try:
                got = capture_keyboard_binding(timeout_s=timeout)
            except Exception as e:  # noqa: BLE001
                got = None
                self._json(200, {"binding": "", "error": str(e)})
                return
            self._json(200, got or {"binding": "", "error": "nothing detected"})
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
