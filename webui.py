"""Web UI for the F1 race engineer.

Single-process, stdlib-only HTTP server (ThreadingHTTPServer + http.server) that:
  - runs the UDP receiver in a background thread, keeping a TelemetryState
  - serves a small HTML page (telemetry panel + question box)
  - exposes /api/state  -> current summary + stats
  - exposes /api/ask    -> POST {question} -> DeepSeek answer

No Flask/FastAPI dependency; keeps the footprint tiny.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from engineer import Engineer
from receiver import DEFAULT_PORT, PACKETS_CONSUMED, TelemetryReceiver
from recorder import SessionRecorder
from state import TelemetryState
from summariser import Summariser

PAGE = r"""<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>F1 Race Engineer</title>
<style>
  :root { --bg:#0f1115; --panel:#171a21; --line:#2a2f3a; --txt:#e6e9ef; --dim:#8a93a6; --accent:#4ea1ff; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--txt); font:14px/1.5 "Segoe UI",system-ui,sans-serif; }
  .wrap { display:grid; grid-template-columns: 1fr 1fr; gap:16px; padding:16px; max-width:1200px; margin:0 auto; }
  h1 { font-size:18px; margin:0 0 12px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px; }
  .row { display:flex; justify-content:space-between; padding:3px 0; border-bottom:1px dashed #22262f; }
  .row:last-child { border-bottom:none; }
  .k { color:var(--dim); }
  .v { font-variant-numeric: tabular-nums; }
  .notes { margin-top:10px; }
  .note { background:#20262f; border-left:3px solid #d8a03a; padding:6px 8px; border-radius:4px; margin:4px 0; }
  .badge { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; }
  .live { background:#1d3a25; color:#6ee787; }
  .wait { background:#3a3320; color:#e7d16e; }
  #log { height:320px; overflow-y:auto; font-size:13px; }
  .msg { margin:8px 0; }
  .msg.me { text-align:right; }
  .msg.me .bubble { background:#243447; }
  .msg.ai .bubble { background:#1f2a1f; }
  .bubble { display:inline-block; padding:8px 12px; border-radius:10px; max-width:85%; text-align:left; }
  .qbar { display:flex; gap:8px; margin-top:12px; }
  input[type=text] { flex:1; background:#0d0f13; border:1px solid var(--line); color:var(--txt); padding:10px; border-radius:8px; }
  button { background:var(--accent); border:none; color:#04121f; font-weight:600; padding:10px 16px; border-radius:8px; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  .quick { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
  .quick button { background:#232a36; color:var(--txt); font-weight:400; font-size:12px; padding:6px 10px; }
  .meta { color:var(--dim); font-size:12px; margin-top:8px; }
  @media (max-width:820px){ .wrap{ grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="wrap">
  <div class="panel">
    <h1>遥测面板 <span id="conn" class="badge wait">等待数据</span></h1>
    <div id="facts"></div>
    <div class="notes" id="notes"></div>
    <div class="meta" id="meta"></div>
    <div style="margin-top:14px;">
      <div style="color:var(--dim);font-size:12px;margin-bottom:6px;">全场排名</div>
      <div id="board"></div>
    </div>
  </div>
  <div class="panel">
    <h1>车队无线电
      <a href="/api/export_txt" style="float:right;font-size:12px;font-weight:400;color:var(--accent);text-decoration:none;border:1px solid var(--line);padding:4px 10px;border-radius:8px;margin-left:6px;">导出TXT</a>
      <a href="/api/export" style="float:right;font-size:12px;font-weight:400;color:var(--dim);text-decoration:none;border:1px solid var(--line);padding:4px 10px;border-radius:8px;">导出JSON</a>
    </h1>
    <div id="log"></div>
    <div class="qbar">
      <input type="text" id="q" placeholder="问点什么…（例：我圈速多少）" autocomplete="off">
      <button id="send">问</button>
    </div>
    <div class="quick">
      <button data-q="我当前圈速和最快圈差多少">圈速差</button>
      <button data-q="我的轮胎情况怎么样">轮胎</button>
      <button data-q="油量够跑完吗">油量</button>
      <button data-q="我现在排第几">位置</button>
      <button data-q="我哪里损失了时间">损失时间</button>
    </div>
    <div class="meta">快捷键: Enter 发送</div>
  </div>
</div>
<script>
const FACTS_CN = {
  lap:"圈数", position:"位置", current_lap_time:"当前圈", last_lap_time:"上一圈",
  best_lap_time:"最快圈", delta_to_best:"vs最快圈", sector1:"S1", sector2:"S2", sector3:"S3",
  gap_to_front:"距前车", gap_to_leader:"距领先", speed_kph:"速度(kph)", gear:"档位",
  tyre_compound:"轮胎", tyre_age_laps:"胎龄(圈)", fuel_kg:"油量(kg)", fuel_laps_left:"油量圈数",
  fuel_rate_kg_per_lap:"油耗(kg/圈)", fuel_surplus_laps:"剩余圈数", predicted_final_fuel_kg:"预测完赛油",
  ers_energy_j:"ERS", drs_allowed:"DRS", active_aero:"空动", overtake_available:"超车可用", overtake_active:"超车激活"
};
function esc(s){ return (s+"").replace(/[<>&]/g, c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c])); }
function renderFacts(facts){
  const el = document.getElementById("facts");
  let html = "";
  for (const [k,label] of Object.entries(FACTS_CN)){
    if (!(k in facts)) continue;
    let v = facts[k];
    if (v === null || v === "" || v === "-") continue;
    if (k === "tyre_temp_c" || Array.isArray(v)) continue;
    html += `<div class="row"><span class="k">${label}</span><span class="v">${esc(v)}</span></div>`;
  }
  if (facts.tyre_temp_c) html += `<div class="row"><span class="k">胎温(FL FR RL RR)</span><span class="v">${esc(facts.tyre_temp_c.join(" / "))}</span></div>`;
  el.innerHTML = html || "<div class='k'>暂无数据</div>";
}
function renderNotes(notes){
  const el = document.getElementById("notes");
  el.innerHTML = (notes||[]).map(n=>`<div class="note">${esc(n)}</div>`).join("");
}
function renderBoard(rows){
  const el = document.getElementById("board");
  if (!rows || !rows.length){ el.innerHTML = "<span class='k'>-</span>"; return; }
  let html = `<div class="row"><span class="k">P 车手</span><span class="k">轮胎  落后</span></div>`;
  for (const r of rows){
    const me = r.is_player ? "style='color:var(--accent);font-weight:700'" : "";
    const gap = r.position === 1 ? "领先" : "+" + ((r.gap_to_leader_ms||0)/1000).toFixed(1) + "s";
    const tyre = r.tyre || "-";
    html += `<div class="row" ${me}><span>${r.position}. ${esc(r.driver||"-")}</span><span class="v">${esc(tyre)}  ${gap}</span></div>`;
  }
  el.innerHTML = html;
}
function addMsg(who, text){
  const log = document.getElementById("log");
  const d = document.createElement("div");
  d.className = "msg " + who;
  d.innerHTML = `<span class="bubble">${esc(text)}</span>`;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}
async function poll(){
  try {
    const r = await fetch("/api/state");
    const d = await r.json();
    const sm = d.summary;
    renderFacts(sm.facts);
    renderNotes(sm.notes);
    renderBoard(sm.leaderboard);
    const conn = document.getElementById("conn");
    const live = d.stats.accepted > 0 && d.summary.facts.lap;
    conn.className = "badge " + (live ? "live" : "wait");
    conn.textContent = live ? "比赛中" : "等待数据";
    document.getElementById("meta").textContent =
      `已收 ${d.stats.accepted} 包 · 丢 ${d.stats.dropped_gate} · 错误 ${JSON.stringify(d.packet_errors||{})}`;
  } catch(e){}
}
async function ask(){
  const input = document.getElementById("q");
  const q = input.value.trim();
  if (!q) return;
  addMsg("me", q);
  input.value = "";
  const btn = document.getElementById("send");
  btn.disabled = true;
  addMsg("ai", "…");
  const log = document.getElementById("log");
  const pending = log.lastChild;
  try {
    const r = await fetch("/api/ask", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({question:q})});
    const d = await r.json();
    pending.remove();
    addMsg("ai", d.answer || ("[" + (d.error||"error") + "]"));
    if (d.usage) console.log("usage", d.usage);
  } catch(e){
    pending.remove();
    addMsg("ai", "[请求失败] " + e);
  } finally { btn.disabled = false; input.focus(); }
}
document.getElementById("send").onclick = ask;
document.getElementById("q").addEventListener("keydown", e => { if (e.key === "Enter") ask(); });
document.querySelectorAll(".quick button").forEach(b => b.onclick = () => {
  document.getElementById("q").value = b.dataset.q; ask();
});
setInterval(poll, 1000); poll();
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "F1TR/0.1"

    def log_message(self, *args):  # silence default logging
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            ctx = self.server.ctx  # type: ignore[attr-defined]
            snap = ctx["state"].snapshot()
            # Continuously persist completed laps so nothing is lost if the
            # window is closed without exporting.
            try:
                ctx["recorder"].record_state(snap)
            except Exception:
                pass
            payload = {
                "summary": ctx["summariser"].summarise(snap),
                "stats": ctx["receiver"].stats(),
                "packet_errors": snap.get("packet_errors", {}),
            }
            self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif self.path == "/api/export":
            ctx = self.server.ctx  # type: ignore[attr-defined]
            rec = ctx["recorder"]
            data = json.dumps(rec.data, ensure_ascii=False, indent=2, default=str)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{rec.path.name}"')
            body = data.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/export_txt":
            ctx = self.server.ctx  # type: ignore[attr-defined]
            rec = ctx["recorder"]
            from report_txt import render
            data = render(rec.data)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             f'attachment; filename="{rec.path.stem}.txt"')
            body = data.encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/ask":
            self._send(404, b"not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            question = str(body.get("question", "")).strip()
        except Exception:
            self._send(400, json.dumps({"error": "bad json"}).encode(), "application/json")
            return
        ctx = self.server.ctx  # type: ignore[attr-defined]
        eng: Engineer = ctx["engineer"]
        snap = ctx["state"].snapshot()
        answer = eng.ask(question, snap)
        usage = eng.client.last_usage
        try:
            ctx["recorder"].record_qa(question, answer, usage)
        except Exception:
            pass
        self._send(200, json.dumps({"answer": answer, "error": eng.last_error, "usage": usage},
                                   ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")


def _receiver_thread(state: TelemetryState, receiver: TelemetryReceiver, logger) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(receiver.run())
    except Exception as e:  # noqa: BLE001
        logger.error("receiver stopped: %r", e)


def serve(port: int = 20777, web_port: int = 8765, logger: Optional[logging.Logger] = None) -> None:
    logger = logger or logging.getLogger("f1_tr.web")
    state = TelemetryState(error_logger=logger)
    # Only the packet types TelemetryState consumes; the rest cost a header
    # parse and are dropped (see receiver.PACKETS_CONSUMED).
    receiver = TelemetryReceiver(state, port=port, bind_ip="127.0.0.1",
                                 interested=PACKETS_CONSUMED, logger=logger)
    engineer = Engineer()

    t = threading.Thread(target=_receiver_thread, args=(state, receiver, logger), daemon=True)
    t.start()

    httpd = ThreadingHTTPServer(("127.0.0.1", web_port), _Handler)
    httpd.ctx = {  # type: ignore[attr-defined]
        "state": state,
        "receiver": receiver,
        "summariser": Summariser(),
        "engineer": engineer,
        "recorder": SessionRecorder(),
    }
    logger.info("web UI on http://127.0.0.1:%s  (UDP %s)", web_port, port)
    print(f"\n>>> Open http://127.0.0.1:{web_port}  (UDP listening on {port})", flush=True)
    print(f">>> Session recording to {httpd.ctx['recorder'].path}\n", flush=True)
    httpd.serve_forever()
