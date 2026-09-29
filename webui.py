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

import audio
from app import build_app
from config import get_config
from engineer import Engineer
from llm_client import LLMError, make_llm
from profiles import PROFILES
from receiver import TelemetryReceiver
from recorder import SessionRecorder
from summariser import Summariser
from voice import VoiceLink

# Hard cap for a voice question upload (~1 minute of opus audio, generous).
MAX_VOICE_BYTES = 5 * 1024 * 1024
# Caps for the text Q&A endpoint (T11 hardening).
MAX_QUESTION_CHARS = 500
MAX_BODY_BYTES = 64 * 1024
# Only one LLM request in flight at a time; extra callers get 429.
_ASK_SEMAPHORE = threading.Semaphore(1)

APP_VERSION = "0.4.2"
_RELEASES_API = "https://api.github.com/repos/cewtoad/FI/releases/latest"
_version_cache: Dict[str, Any] = {"at": 0.0, "data": None}


def _check_update() -> Dict[str, Any]:
    """Return {current, latest, update_available}; cached for 6 hours.

    Best-effort only: any network failure returns update_available=False so the
    panel never blocks or errors on a missing internet connection.
    """
    import time
    import urllib.request

    now = time.time()
    cached = _version_cache.get("data")
    if cached is not None and (now - _version_cache.get("at", 0)) < 6 * 3600:
        return cached
    result = {"current": APP_VERSION, "latest": None, "update_available": False}
    try:
        req = urllib.request.Request(_RELEASES_API,
                                     headers={"User-Agent": "F1RaceEngineer"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = str(data.get("tag_name") or "").lstrip("v")
        if latest:
            result["latest"] = latest
            result["update_available"] = _version_tuple(latest) > _version_tuple(APP_VERSION)
    except Exception:
        pass
    _version_cache["at"] = now
    _version_cache["data"] = result
    return result


def _json_safe(o):
    """Replace non-finite floats with None: json.dumps would emit the invalid
    token ``Infinity``/``NaN`` and the page's ``r.json()`` would throw."""
    if isinstance(o, float) and (o != o or o in (float("inf"), float("-inf"))):
        return None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


def _version_tuple(v: str):
    parts = []
    for p in str(v).split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts)

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
  .mic { background:#232a36; color:var(--txt); font-size:16px; padding:10px 14px; }
  .mic.rec { background:#7a2a2a; color:#fff; animation:pulse 1s infinite; }
  @keyframes pulse { 50% { opacity:.55; } }
  .meta { color:var(--dim); font-size:12px; margin-top:8px; }
  .setup { background:#3a2f16; border:1px solid #7a6320; border-radius:10px; padding:12px 16px; margin-bottom:16px; display:none; }
  .setup h2 { font-size:14px; margin:0 0 6px; color:#e7d16e; }
  .setup input, .setup select { background:#0d0f13; border:1px solid var(--line); color:var(--txt); padding:8px; border-radius:6px; width:100%; margin:4px 0; }
  .setup .row2 { display:flex; gap:8px; }
  .setup code { background:#0d0f13; padding:1px 5px; border-radius:4px; color:var(--accent); }
  .ok { color:#6ee787; } .bad { color:#e77; }
  details.setupbox { margin-top:10px; }
  details.setupbox summary { cursor:pointer; color:var(--accent); font-size:12px; }
  @media (max-width:820px){ .wrap{ grid-template-columns:1fr; } }
</style>
</head>
<body>
<div class="wrap" style="grid-template-columns:1fr;">
  <div class="setup" id="update" style="background:#16293a;border-color:#2b5a7a;display:none;">
    <span style="color:#8ecbff;"><span data-i18n="update.found">发现新版本</span> <b id="newver"></b><span id="updCur"></span></span>
    <a id="dlLink" href="https://github.com/cewtoad/FI/releases/latest" target="_blank"
       style="color:var(--accent);margin-left:8px;" data-i18n="update.dl">前往下载 →</a>
  </div>
  <div class="setup" id="setup" style="display:none">
    <h2>⚙ <span data-i18n="ai.title">AI 设置（不填也能用本地问答）</span>
      <a href="#" id="setupClose" style="float:right;font-size:12px;font-weight:400;color:var(--dim);text-decoration:none;" data-i18n="common.close">关闭 ✕</a>
    </h2>
    <div style="font-size:12px;color:var(--dim);margin-bottom:6px;" data-i18n="ai.desc">
      支持任意 OpenAI 兼容端点（DeepSeek / OpenAI / Moonshot / Qwen / 本地 Ollama）。名次、圈速、油量、胎温、损伤等高频问题无需 key 即可回答。
    </div>
    <div class="row2">
      <input type="text" id="setBase" placeholder="Base URL（如 https://api.deepseek.com）" data-i18n-ph="ai.base">
      <input type="text" id="setModel" placeholder="模型（如 deepseek-flash）" data-i18n-ph="ai.model">
    </div>
    <input type="password" id="setKey" placeholder="API Key（sk-...）" data-i18n-ph="ai.key">
    <div class="row2" style="margin-top:6px;">
      <button id="setSave" data-i18n="ai.save">保存并测试连接</button>
    </div>
    <div class="meta" id="setMsg"></div>
  </div>
  <div class="setup" id="featBox" style="display:none">
    <h2>🎚 <span data-i18n="feat.title">功能设置</span>
      <a href="#" id="featClose" style="float:right;font-size:12px;font-weight:400;color:var(--dim);text-decoration:none;" data-i18n="common.close">关闭 ✕</a>
    </h2>
    <details class="setupbox">
      <summary data-i18n="audio.title">🎤🔊 语音设备（默认跟随系统正在使用的设备）</summary>
      <div class="row2" style="margin-top:6px;align-items:center;">
        <span style="font-size:12px;color:var(--dim);flex:0 0 62px;" data-i18n="audio.mic">麦克风</span>
        <select id="setMic" style="width:100%;background:#232a36;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:6px 8px;"></select>
      </div>
      <div class="row2" style="margin-top:6px;align-items:center;">
        <span style="font-size:12px;color:var(--dim);flex:0 0 62px;" data-i18n="audio.spk">播报输出</span>
        <select id="setSpk" style="width:100%;background:#232a36;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:6px 8px;"></select>
      </div>
      <div class="row2" style="margin-top:6px;">
        <button id="audRefresh" style="padding:5px 12px;" data-i18n="audio.refresh">🔄 重新检测设备</button>
      </div>
      <div style="font-size:12px;color:var(--dim);margin-top:6px;">
        <span data-i18n="audio.note">默认自动使用系统当前设备——换耳机、换电脑无需改配置，拔插/切换默认设备后下一次语音即生效（也可在此固定）。</span>
        <br><span data-i18n="audio.now">当前使用：</span><span id="audNow" style="color:var(--txt);"></span>
      </div>
    </details>
    <details class="setupbox">
      <summary data-i18n="udp.title">游戏内 UDP 遥测怎么设？</summary>
      <div style="font-size:12px;line-height:1.9;margin-top:6px;" data-i18n-html="udp.body">
        游戏 <b>设置 → UDP 遥测</b>：<br>
        • UDP 遥测：<code>开启</code><br>
        • UDP IP：<code>127.0.0.1</code>　• UDP 端口：<code>20777</code><br>
        • UDP 赛制：<code>2026</code>（或与你游戏版本一致）<br>
        • <b>“你的遥测”保持 <code>受限</code></b> —— 改后可能收不到数据，需重启游戏。
      </div>
    </details>
    <div style="font-size:12px;color:var(--dim);margin:10px 0 2px;" data-i18n="feat.hint">
      主动播报 / 语音 / 推演 / 复盘 —— 改这里立即生效并写入 .env（部分项需重启）。
    </div>
    <div id="featForm" style="margin-top:6px;"></div>
    <div class="row2" style="margin-top:6px;">
      <button id="featSave" data-i18n="feat.save">保存开关</button>
      <a href="http://127.0.0.1:8766" target="_blank"
         style="align-self:center;font-size:12px;color:var(--accent);text-decoration:none;" data-i18n="feat.full">
         打开完整设置页(语音包 / 按键 / 高级) →</a>
    </div>
    <div class="meta" id="featMsg"></div>
  </div>
</div>
<div class="wrap">
  <div class="panel">
    <h1><span data-i18n="panel.telemetry">遥测面板</span> <span id="conn" class="badge wait" data-i18n="conn.wait">等待数据</span>
      <span style="float:right;font-size:12px;font-weight:400;">
        <a href="#" id="langZh" style="color:var(--dim);text-decoration:none;border:1px solid var(--line);padding:4px 8px;border-radius:8px;margin-left:6px;">中文</a>
        <a href="#" id="langEn" style="color:var(--dim);text-decoration:none;border:1px solid var(--line);padding:4px 8px;border-radius:8px;margin-left:4px;">EN</a>
        <a href="#" id="openSet" style="color:var(--dim);text-decoration:none;border:1px solid var(--line);padding:4px 10px;border-radius:8px;margin-left:6px;" data-i18n="nav.ai">AI 设置</a>
        <a href="#" id="openFeat" style="color:var(--dim);text-decoration:none;border:1px solid var(--line);padding:4px 10px;border-radius:8px;margin-left:6px;" data-i18n="nav.feat">功能设置</a>
      </span>
    </h1>
    <div id="alertbar" style="display:none"></div>
    <div id="facts"></div>
    <div class="notes" id="notes"></div>
    <div class="meta" id="meta"></div>
    <div style="margin-top:14px;">
      <div style="color:var(--dim);font-size:12px;margin-bottom:6px;" data-i18n="panel.board">全场排名</div>
      <div id="board"></div>
    </div>
  </div>
  <div class="panel">
    <h1><span data-i18n="panel.radio">车队无线电</span>
      <a href="/api/export_txt" style="float:right;font-size:12px;font-weight:400;color:var(--accent);text-decoration:none;border:1px solid var(--line);padding:4px 10px;border-radius:8px;margin-left:6px;" data-i18n="nav.exportTxt">导出TXT</a>
      <a href="/api/export" style="float:right;font-size:12px;font-weight:400;color:var(--dim);text-decoration:none;border:1px solid var(--line);padding:4px 10px;border-radius:8px;" data-i18n="nav.exportJson">导出JSON</a>
    </h1>
    <div id="log"></div>
    <div class="qbar">
      <input type="text" id="q" placeholder="问点什么…（例：我圈速多少）" data-i18n-ph="ask.ph" autocomplete="off">
      <button id="mic" class="mic" style="display:none" title="按住说话，松开发送">🎤</button>
      <button id="send" data-i18n="ask.send">问</button>
    </div>
    <div class="quick">
      <button data-q="我当前圈速和最快圈差多少" data-i18n="q.lap">圈速差</button>
      <button data-q="我的轮胎情况怎么样" data-i18n="q.tyre">轮胎</button>
      <button data-q="油量够跑完吗" data-i18n="q.fuel">油量</button>
      <button data-q="我现在排第几" data-i18n="q.pos">位置</button>
      <button data-q="我哪里损失了时间" data-i18n="q.loss">损失时间</button>
    </div>
    <div class="meta" id="voicehint" style="display:none" data-i18n="ask.voicehint">🎤 按住说话 · 松开发送 · Esc 取消</div>
    <div class="meta" data-i18n="ask.enter">快捷键: Enter 发送</div>
  </div>
</div>
<script>
// ---- i18n (zh / en), persisted in localStorage as f1tr_lang ----
const I18N = {
  zh: {
    "update.found":"发现新版本", "update.dl":"前往下载 →", "update.cur":"（当前 {v}）",
    "common.close":"关闭 ✕", "common.on":"开", "common.off":"关",
    "ai.title":"AI 设置（不填也能用本地问答）",
    "ai.desc":"支持任意 OpenAI 兼容端点（DeepSeek / OpenAI / Moonshot / Qwen / 本地 Ollama）。名次、圈速、油量、胎温、损伤等高频问题无需 key 即可回答。",
    "ai.base":"Base URL（如 https://api.deepseek.com）", "ai.model":"模型（如 deepseek-flash）",
    "ai.key":"API Key（sk-...）", "ai.save":"保存并测试连接",
    "ai.saving":"保存中…", "ai.testing":"已保存，测试连接中…",
    "ai.ok":"✓ 连接成功，AI 已就绪", "ai.fail":"连接失败：{e}（本地问答仍可用）",
    "feat.title":"功能设置", "feat.hint":"主动播报 / 语音 / 推演 / 复盘 —— 改这里立即生效并写入 .env（部分项需重启）。",
    "feat.save":"保存开关", "feat.full":"打开完整设置页(语音包 / 按键 / 高级) →",
    "feat.saving":"保存中…", "feat.saved":"✓ 已保存并生效", "feat.partial":"部分失败：",
    "audio.title":"🎤🔊 语音设备（默认跟随系统正在使用的设备）",
    "audio.mic":"麦克风", "audio.spk":"播报输出", "audio.refresh":"🔄 重新检测设备",
    "audio.note":"默认自动使用系统当前设备——换耳机、换电脑无需改配置，拔插/切换默认设备后下一次语音即生效（也可在此固定）。",
    "audio.now":"当前使用：", "audio.follow":"跟随系统当前设备", "audio.cur":"（当前: {n}）",
    "audio.sysdefault":"（系统默认）", "audio.none":"无",
    "udp.title":"游戏内 UDP 遥测怎么设？",
    "udp.body":"游戏 <b>设置 → UDP 遥测</b>：<br>• UDP 遥测：<code>开启</code><br>• UDP IP：<code>127.0.0.1</code>　• UDP 端口：<code>20777</code><br>• UDP 赛制：<code>2026</code>（或与你游戏版本一致）<br>• <b>“你的遥测”保持 <code>受限</code></b> —— 改后可能收不到数据，需重启游戏。",
    "panel.telemetry":"遥测面板", "panel.board":"全场排名", "panel.radio":"车队无线电",
    "conn.wait":"等待数据", "conn.live":"比赛中",
    "nav.ai":"AI 设置", "nav.feat":"功能设置", "nav.exportTxt":"导出TXT", "nav.exportJson":"导出JSON",
    "ask.ph":"问点什么…（例：我圈速多少）", "ask.send":"问", "ask.voicehint":"🎤 按住说话 · 松开发送 · Esc 取消", "ask.enter":"快捷键: Enter 发送",
    "q.lap":"圈速差", "q.tyre":"轮胎", "q.fuel":"油量", "q.pos":"位置", "q.loss":"损失时间",
    "meta.packets":"已收 {a} 包 · 丢 {d} · 错误 {e}",
    "board.h":"P 车手", "board.h2":"轮胎  落后", "board.lead":"领先",
    "ui.noanswer":"" 
  },
  en: {
    "update.found":"New version", "update.dl":"Download →", "update.cur":" (current {v})",
    "common.close":"Close ✕", "common.on":"On", "common.off":"Off",
    "ai.title":"AI settings (optional — local answers work without a key)",
    "ai.desc":"Any OpenAI-compatible endpoint (DeepSeek / OpenAI / Moonshot / Qwen / local Ollama). Position, lap times, fuel, tyre temps and damage are answered locally without a key.",
    "ai.base":"Base URL (e.g. https://api.deepseek.com)", "ai.model":"Model (e.g. deepseek-flash)",
    "ai.key":"API Key (sk-...)", "ai.save":"Save & test connection",
    "ai.saving":"Saving…", "ai.testing":"Saved, testing connection…",
    "ai.ok":"✓ Connected, AI ready", "ai.fail":"Connection failed: {e} (local answers still work)",
    "feat.title":"Feature settings", "feat.hint":"Radio / voice / race model / debrief — saved to .env and applied live (some need a restart).",
    "feat.save":"Save", "feat.full":"Open full settings page (voice / keys / advanced) →",
    "feat.saving":"Saving…", "feat.saved":"✓ Saved", "feat.partial":"Some failed: ",
    "audio.title":"🎤🔊 Audio devices (default: follow the system device)",
    "audio.mic":"Microphone", "audio.spk":"Speaker", "audio.refresh":"🔄 Re-scan devices",
    "audio.note":"By default the current system device is used — plugging/switching the default takes effect on the next voice take (you can also pin one here).",
    "audio.now":"In use: ", "audio.follow":"Follow system default", "audio.cur":" (current: {n})",
    "audio.sysdefault":" (system default)", "audio.none":"none",
    "udp.title":"How to enable in-game UDP telemetry",
    "udp.body":"Game <b>Settings → UDP Telemetry</b>:<br>• UDP Telemetry: <code>On</code><br>• UDP IP: <code>127.0.0.1</code>　• UDP Port: <code>20777</code><br>• UDP Format: <code>2026</code> (match your game)<br>• Keep <b>“Your Telemetry” = <code>Restricted</code></b> — changing it may break reception; restart the game.",
    "panel.telemetry":"Telemetry", "panel.board":"Leaderboard", "panel.radio":"Team radio",
    "conn.wait":"waiting", "conn.live":"LIVE",
    "nav.ai":"AI settings", "nav.feat":"Features", "nav.exportTxt":"Export TXT", "nav.exportJson":"Export JSON",
    "ask.ph":"Ask something… (e.g. what's my lap time)", "ask.send":"Ask", "ask.voicehint":"🎤 Hold to talk · release to send · Esc to cancel", "ask.enter":"Shortcut: Enter to send",
    "q.lap":"Lap delta", "q.tyre":"Tyres", "q.fuel":"Fuel", "q.pos":"Position", "q.loss":"Time lost",
    "meta.packets":"rx {a} · dropped {d} · errors {e}",
    "board.h":"P Driver", "board.h2":"Tyre  Gap", "board.lead":"leader",
    "ui.noanswer":""
  }
};
let LANG = localStorage.getItem('f1tr_lang') || 'zh';
function t(k, vars){
  let s = (I18N[LANG] && I18N[LANG][k]) || (I18N.zh[k] !== undefined ? I18N.zh[k] : k);
  if (vars){ for (const key in vars){ s = s.replace('{'+key+'}', vars[key]); } }
  return s;
}
function pick(o, base){
  return LANG === 'en' ? (o[base+'_en'] || o[base] || '') : (o[base] || o[base+'_en'] || '');
}
function applyI18n(){
  document.documentElement.lang = (LANG === 'en' ? 'en' : 'zh');
  document.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.getAttribute('data-i18n')); });
  document.querySelectorAll('[data-i18n-ph]').forEach(el => { el.placeholder = t(el.getAttribute('data-i18n-ph')); });
  document.querySelectorAll('[data-i18n-html]').forEach(el => { el.innerHTML = t(el.getAttribute('data-i18n-html')); });
  const z=document.getElementById('langZh'), e=document.getElementById('langEn');
  if (z) z.style.color = LANG==='zh' ? 'var(--accent)' : 'var(--dim)';
  if (e) e.style.color = LANG==='en' ? 'var(--accent)' : 'var(--dim)';
  featSchema = null; const ff=document.getElementById('featForm'); if (ff) ff.innerHTML='';
  loadAudio();
  loadFeatures();
}
function setLang(l){ LANG = l; localStorage.setItem('f1tr_lang', l); applyI18n(); }
const FACTS_CN = {
  lap:"圈数", position:"位置", current_lap_time:"当前圈", last_lap_time:"上一圈",
  best_lap_time:"最快圈", delta_to_best:"vs最快圈", sector1:"S1", sector2:"S2", sector3:"S3",
  gap_to_front:"距前车", gap_to_leader:"距领先", speed_kph:"速度(kph)", gear:"档位",
  tyre_compound:"轮胎", tyre_age_laps:"胎龄(圈)", fuel_kg:"油量(kg)", fuel_laps_left:"油量圈数",
  fuel_rate_kg_per_lap:"油耗(kg/圈)", fuel_surplus_laps:"剩余圈数", predicted_final_fuel_kg:"预测完赛油",
  ers_energy_j:"ERS", drs_allowed:"DRS", active_aero:"空动", overtake_available:"超车可用", overtake_active:"超车激活",
  total_laps:"总圈数", laps_remaining:"剩余圈数", session_time_left_s:"剩余时间(s)", air_temp_c:"气温",
  track_temp_c:"赛道温度", rain_percentage:"降雨概率(%)", pit_stops:"进站次数", lockup:"抱死"
};
const FACTS_EN = {
  lap:"Lap", position:"Pos", current_lap_time:"Current", last_lap_time:"Last",
  best_lap_time:"Best", delta_to_best:"vs best", sector1:"S1", sector2:"S2", sector3:"S3",
  gap_to_front:"Gap ahead", gap_to_leader:"Gap to leader", speed_kph:"Speed(kph)", gear:"Gear",
  tyre_compound:"Tyre", tyre_age_laps:"Tyre age(laps)", fuel_kg:"Fuel(kg)", fuel_laps_left:"Fuel laps",
  fuel_rate_kg_per_lap:"Fuel/lap", fuel_surplus_laps:"Surplus laps", predicted_final_fuel_kg:"Pred. finish fuel",
  ers_energy_j:"ERS", drs_allowed:"DRS", active_aero:"Aero", overtake_available:"Overtake avail", overtake_active:"Overtake active",
  total_laps:"Total laps", laps_remaining:"Laps left", session_time_left_s:"Time left(s)", air_temp_c:"Air temp",
  track_temp_c:"Track temp", rain_percentage:"Rain(%)", pit_stops:"Pit stops", lockup:"Lockup"
};
function factLabel(k){ const M = (LANG==='en'?FACTS_EN:FACTS_CN); return M[k] || k; }
function esc(s){ return (s+"").replace(/[<>&]/g, c=>({"<":"&lt;",">":"&gt;","&":"&amp;"}[c])); }
function renderFacts(facts){
  const el = document.getElementById("facts");
  let html = "";
  for (const k in facts){
    let v = facts[k];
    if (v === null || v === "" || v === "-") continue;
    if (k === "tyre_temp_c" || Array.isArray(v)) continue;
    if (!(k in FACTS_CN) && !(k in FACTS_EN)) continue;   // show only known keys
    html += `<div class="row"><span class="k">${factLabel(k)}</span><span class="v">${esc(v)}</span></div>`;
  }
  if (facts.tyre_temp_c) html += `<div class="row"><span class="k">${LANG==='en'?'Tyre °C (FL FR RL RR)':'胎温(FL FR RL RR)'}</span><span class="v">${esc(facts.tyre_temp_c.join(" / "))}</span></div>`;
  el.innerHTML = html || `<div class='k'>${LANG==='en'?'no data':'暂无数据'}</div>`;
}
function renderNotes(notes){
  const el = document.getElementById("notes");
  el.innerHTML = (notes||[]).map(n=>`<div class="note">${esc(n)}</div>`).join("");
}
function renderBoard(rows){
  const el = document.getElementById("board");
  if (!rows || !rows.length){ el.innerHTML = "<span class='k'>-</span>"; return; }
  let html = `<div class="row"><span class="k">${t('board.h')}</span><span class="k">${t('board.h2')}</span></div>`;
  for (const r of rows){
    const me = r.is_player ? "style='color:var(--accent);font-weight:700'" : "";
    const gap = r.position === 1 ? t('board.lead') : "+" + ((r.gap_to_leader_ms||0)/1000).toFixed(1) + "s";
    const tyre = r.tyre || "-";
    html += `<div class="row" ${me}><span>${r.position}. ${esc(r.driver||"-")}</span><span class="v">${esc(tyre)}  ${gap}</span></div>`;
  }
  el.innerHTML = html;
}
function renderAlerts(alerts){
  const el = document.getElementById("alertbar");
  if (!alerts || !alerts.length){ el.style.display = "none"; el.innerHTML = ""; return; }
  // Newest last from the server; show the most recent few, newest on top.
  const recent = alerts.slice(-4).reverse();
  const color = p => p === 0 ? "#ff5c5c" : (p === 1 ? "#ffb020" : "#7ab8ff");
  el.style.display = "";
  el.innerHTML = recent.map(a =>
    `<div style="border-left:3px solid ${color(a.priority)};background:#101f2e;`+
    `padding:6px 10px;margin:6px 0;border-radius:6px;font-size:13px;">`+
    `<span style="color:${color(a.priority)};font-weight:700;">${
      a.priority===0?'P0':(a.priority===1?'P1':'P2')}</span> `+
    `${esc(a.text||"")}</div>`).join("");
}
function addMsg(who, text){
  const log = document.getElementById("log");
  const d = document.createElement("div");
  d.className = "msg " + who;
  d.innerHTML = `<span class="bubble">${esc(text)}</span>`;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
}
let llmKnown = null;
let setupDismissed = false;
async function refreshSetup(){
  try {
    const r = await fetch("/api/llm");
    const d = await r.json();
    const eng = d.engineer || {};
    const noKey = !eng.base_url || !eng.model;
    // Only do first-run onboarding: show the AI panel once when no key is set.
    // Never auto-hide it — that used to close the panel the user just opened.
    if (llmKnown === null && noKey && !setupDismissed){
      document.getElementById("setup").style.display = "block";
      if (!document.getElementById("setBase").value) document.getElementById("setBase").value = eng.base_url || "https://api.deepseek.com";
      if (!document.getElementById("setModel").value) document.getElementById("setModel").value = eng.model || "deepseek-flash";
    }
    llmKnown = true;
  } catch(e){}
}
async function saveSetup(){
  const base = document.getElementById("setBase").value.trim();
  const model = document.getElementById("setModel").value.trim();
  const key = document.getElementById("setKey").value.trim();
  const msg = document.getElementById("setMsg");
  msg.className = "meta"; msg.textContent = t("ai.saving");
  try {
    const body = {};
    if (base) body.base_url = base;
    if (model) body.model = model;
    if (key) body.api_key = key;
    await fetch("/api/llm", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
    msg.textContent = t("ai.testing");
    const r = await fetch("/api/models");
    if (r.ok){ msg.className = "meta ok"; msg.textContent = t("ai.ok"); document.getElementById("setKey").value = ""; }
    else { const e = await r.json(); msg.className = "meta bad"; msg.textContent = t("ai.fail", {e:(e.error||r.status)}); }
  } catch(e){ msg.className = "meta bad"; msg.textContent = t("ai.fail", {e:e}); }
}
document.getElementById("setSave").onclick = saveSetup;
function showPanel(id){
  document.getElementById("setup").style.display = (id === "setup") ? "block" : "none";
  document.getElementById("featBox").style.display = (id === "featBox") ? "block" : "none";
}
document.getElementById("setupClose").onclick = (ev) => { ev.preventDefault(); setupDismissed = true; showPanel(""); };
document.getElementById("featClose").onclick = (ev) => { ev.preventDefault(); showPanel(""); };
document.getElementById("openSet").onclick = (ev) => { ev.preventDefault(); setupDismissed = true; showPanel("setup"); window.scrollTo(0,0); };
document.getElementById("openFeat").onclick = (ev) => { ev.preventDefault(); showPanel("featBox"); loadFeatures(); loadAudio(); window.scrollTo(0,0); };
document.getElementById("langZh").onclick = (ev) => { ev.preventDefault(); setLang('zh'); };
document.getElementById("langEn").onclick = (ev) => { ev.preventDefault(); setLang('en'); };
// ---- feature toggles (schema-driven) ----
let featSchema = null;
async function loadFeatures(){
  if (featSchema) return;
  try {
    const r = await fetch("/api/settings"); const d = await r.json();
    featSchema = d.settings || [];
    const groups = {};
    for (const s of featSchema){ (groups[s.group] = groups[s.group] || []).push(s); }
    const gnameZh = {ai:"AI / 模型",audio:"音频设备",voice:"语音 / 按键",radio:"主动播报",model:"推演 / 轮胎",debrief:"赛后复盘",general:"通用"};
    const gnameEn = {ai:"AI / Model",audio:"Audio devices",voice:"Voice / PTT",radio:"Proactive radio",model:"Race model / Tyres",debrief:"Debrief",general:"General"};
    let html = "";
    for (const g of Object.keys(groups)){
      const gname = (LANG==='en' ? gnameEn : gnameZh);
      html += `<div style="color:var(--accent);font-size:12px;margin:8px 0 2px;">${gname[g]||g}</div>`;
      for (const s of groups[g]){
        const id = "f_" + s.key;
        let input;
        if (s.type === "bool"){
          input = `<select id="${id}"><option value="1">${t('common.on')}</option><option value="0">${t('common.off')}</option></select>`;
        } else if (s.type === "enum"){
          input = `<select id="${id}">` + s.choices.map(c=>`<option>${c}</option>`).join("") + `</select>`;
        } else if (s.type === "int" || s.type === "float"){
          input = `<input id="${id}" type="number" step="${s.type==="float"?"0.1":"1"}" value="${s.value}">`;
        } else if (s.secret){
          input = `<input id="${id}" type="password" placeholder="${LANG==='en'?'(hidden; leave blank)':'（已隐藏，留空不改）'}" style="width:100%">`;
        } else {
          input = `<input id="${id}" value="${s.value}">`;
        }
        const help = pick(s, "help");
        html += `<div class="row2" style="align-items:flex-start;margin:4px 0;">`
              + `<label style="flex:0 0 210px;font-size:12px;color:var(--dim);">${pick(s,"label")||s.key}`
              + (help?`<div style="color:#7c8794;font-size:11px;line-height:1.4;margin-top:2px;">${esc(help)}</div>`:"")
              + `</label>`
              + `<span style="flex:1">${input}</span></div>`;
      }
    }
    document.getElementById("featForm").innerHTML = html || `<span class='meta'>${LANG==='en'?'nothing to set':'无可调项'}</span>`;
    for (const s of featSchema){
      const el = document.getElementById("f_"+s.key); if (!el) continue;
      if (s.type === "bool"){
        const v = String(s.value).toLowerCase();
        el.value = (v==="1"||v==="true"||v==="on") ? "1" : "0";
      } else if (!s.secret){ el.value = s.value == null ? "" : s.value; }
    }
  } catch(e){}
}
async function saveFeatures(){
  const msg = document.getElementById("featMsg");
  if (!featSchema){ return; }
  msg.className = "meta"; msg.textContent = t("feat.saving");
  const updates = {};
  for (const s of featSchema){
    const el = document.getElementById("f_"+s.key); if (!el) continue;
    if (s.secret && !el.value) continue;   // don't clobber a hidden secret
    updates[s.key] = el.value;
  }
  try {
    const r = await fetch("/api/settings", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(updates)});
    const d = await r.json();
    const errs = Object.keys(d.errors||{});
    if (errs.length){ msg.className = "meta bad"; msg.textContent = t("feat.partial") + JSON.stringify(d.errors); }
    else { msg.className = "meta ok"; msg.textContent = t("feat.saved"); }
  } catch(e){ msg.className = "meta bad"; msg.textContent = t("feat.partial") + e; }
}
document.getElementById("featSave").onclick = saveFeatures;
// ---- audio devices: follow-system by default, pin optional ----
async function loadAudio(){
  try {
    const r = await fetch("/api/audio"); const d = await r.json();
    const fill = (sel, list, cur, active) => {
      sel.innerHTML = "";
      const o = document.createElement("option");
      o.value = "";
      o.textContent = t("audio.follow") + (active && active.name ? t("audio.cur", {n:active.name}) : "");
      sel.appendChild(o);
      for (const dev of (list||[])){
        const op = document.createElement("option");
        op.value = dev.name;
        op.textContent = dev.name + (dev.default ? t("audio.sysdefault") : "");
        if (cur && dev.name === cur) op.selected = true;
        sel.appendChild(op);
      }
    };
    fill(document.getElementById("setMic"), d.input, (d.current||{}).input, (d.active||{}).input);
    fill(document.getElementById("setSpk"), d.output, (d.current||{}).output, (d.active||{}).output);
    const a = d.active || {};
    document.getElementById("audNow").textContent =
      "🎤 " + ((a.input && a.input.name) || t("audio.none")) + " / 🔊 " + ((a.output && a.output.name) || t("audio.none"));
  } catch(e){}
}
["setMic","setSpk"].forEach(id => {
  document.getElementById(id).onchange = async (ev) => {
    const kind = id === "setMic" ? "input" : "output";
    try {
      await fetch("/api/audio", {method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({[kind]: ev.target.value})});
      loadAudio();
    } catch(e){}
  };
});
document.getElementById("openFeat").addEventListener("click", loadAudio);
document.getElementById("audRefresh").onclick = loadAudio;
loadAudio();
async function poll(){
  try {
    const r = await fetch("/api/state");
    const d = await r.json();
    const sm = d.summary;
    renderFacts(sm.facts);
    renderNotes(sm.notes);
    renderBoard(sm.leaderboard);
    renderAlerts(d.alerts || []);
  const conn = document.getElementById("conn");
  const live = d.stats.accepted > 0 && d.summary.facts.lap;
  conn.className = "badge " + (live ? "live" : "wait");
  conn.textContent = live ? t("conn.live") : t("conn.wait");
  const v = d.voice || {};
  window.voiceOn = !!v.stt;
  document.getElementById("mic").style.display = window.voiceOn ? "" : "none";
  document.getElementById("voicehint").style.display = window.voiceOn ? "" : "none";
  document.getElementById("meta").textContent =
      t("meta.packets", {a:d.stats.accepted, d:d.stats.dropped_gate, e:JSON.stringify(d.packet_errors||{})});
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

// ---- voice link (push-to-talk) ----
let recorder = null, recChunks = [], recT0 = 0, voiceBusy = false, recCancel = false;
const micBtn = document.getElementById("mic");
async function startRec(){
  if (voiceBusy || recorder || !window.voiceOn) return;
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    const mt = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
      ? "audio/webm;codecs=opus"
      : (MediaRecorder.isTypeSupported("audio/mp4") ? "audio/mp4" : "");
    recorder = mt ? new MediaRecorder(stream, {mimeType: mt}) : new MediaRecorder(stream);
    recChunks = []; recCancel = false; recT0 = Date.now();
    recorder.ondataavailable = e => { if (e.data && e.data.size) recChunks.push(e.data); };
    recorder.onstop = onRecStop;
    recorder.start();
    micBtn.classList.add("rec"); micBtn.textContent = "⏺";
  } catch(e){ addMsg("ai", "[麦克风不可用] " + e); }
}
function stopRec(cancel){
  recCancel = recCancel || !!cancel;
  if (recorder && recorder.state !== "inactive") recorder.stop();
}
async function onRecStop(){
  const dur = Date.now() - recT0;
  micBtn.classList.remove("rec"); micBtn.textContent = "🎤";
  const stream = recorder ? recorder.stream : null; recorder = null;
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (recCancel || dur < 200) return;          // too short = accidental tap
  const blob = new Blob(recChunks);
  if (!blob.size) return;
  voiceBusy = true; micBtn.disabled = true;
  addMsg("ai", "…");
  const log = document.getElementById("log");
  const pending = log.lastChild;
  try {
    const r = await fetch("/api/ask_voice", {method: "POST",
      headers: {"Content-Type": blob.type || "audio/webm"}, body: blob});
    const d = await r.json();
    pending.remove();
    if (d.error){ addMsg("ai", "[" + d.error + "]"); return; }
    addMsg("me", d.question);
    addMsg("ai", d.answer);
    if (d.audio_b64){
      const a = new Audio("data:" + (d.audio_mime || "audio/mpeg") + ";base64," + d.audio_b64);
      a.play().catch(()=>{});
    }
  } catch(e){
    pending.remove();
    addMsg("ai", "[语音请求失败] " + e);
  } finally { voiceBusy = false; micBtn.disabled = false; }
}
micBtn.addEventListener("pointerdown", e => { e.preventDefault(); startRec(); });
micBtn.addEventListener("pointerup",   e => { e.preventDefault(); stopRec(false); });
micBtn.addEventListener("pointerleave", () => { if (recorder) stopRec(true); });
window.addEventListener("pointerup", () => { if (recorder) stopRec(false); });
window.addEventListener("keydown", e => { if (e.key === "Escape" && recorder) stopRec(true); });
window.addEventListener("blur", () => { if (recorder) stopRec(true); });

async function checkUpdate(){
  try {
    const r = await fetch("/api/version");
    const d = await r.json();
    if (d.update_available){
      document.getElementById("newver").textContent = d.latest;
      document.getElementById("updCur").textContent = t("update.cur", {v:d.current});
      document.getElementById("update").style.display = "block";
    }
  } catch(e){}
}
applyI18n();
setInterval(poll, 1000); poll();
setInterval(refreshSetup, 5000); refreshSetup();
checkUpdate();
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

    def _send_json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _local_only(self) -> bool:
        """True when the request originates from the local machine.

        Config-writing endpoints (/api/llm, /api/audio) must never be reachable
        from the LAN, even if the panel is bound to 0.0.0.0 - otherwise anyone
        on the network could overwrite the API key and persist it to .env.
        """
        addr = self.client_address[0] if self.client_address else ""
        return addr in ("127.0.0.1", "::1", "localhost")

    def _host_ok(self) -> bool:
        """Reject non-local Host headers to blunt DNS-rebinding reads of
        /api/state. Only enforced while bound to loopback."""
        bind = getattr(self.server, "bind_ip", "127.0.0.1")
        if bind not in ("127.0.0.1", "localhost"):
            return True
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        return host in ("127.0.0.1", "localhost", "[::1]", "::1", "")

    def do_GET(self):
        if not self._host_ok():
            self._send(403, b"forbidden host", "text/plain")
            return
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            ctx = self.server.ctx  # type: ignore[attr-defined]
            snap = ctx["state"].snapshot()
            # Laps are now persisted by the receiver hook; keep this as a
            # low-rate backup for the leaderboard/vehicle-status fields.
            try:
                rec = ctx.get("recorder")
                if rec is not None:
                    rec.record_state(snap)
            except Exception:
                pass
            payload = {
                "summary": ctx["summariser"].summarise(snap),
                "stats": ctx["receiver"].stats(),
                "packet_errors": snap.get("packet_errors", {}),
                "voice": {
                    "stt": ctx["voice"].stt_available,
                    "tts": ctx["voice"].tts_available,
                },
                # T5.1: proactive radio alerts (web mode shows an alert bar).
                "alerts": list(ctx.get("alert_log", [])),
                "race_model": snap.get("race_model"),
            }
            self._send(200, json.dumps(_json_safe(payload), ensure_ascii=False,
                                       default=str).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif self.path == "/api/version":
            self._send_json(200, _check_update())
        elif self.path == "/api/llm":
            ctx = self.server.ctx  # type: ignore[attr-defined]
            self._send_json(200, {"engineer": ctx["engineer"].describe()})
        elif self.path == "/api/models":
            ctx = self.server.ctx  # type: ignore[attr-defined]
            try:
                self._send_json(200, {"models": ctx["engineer"].client.model_list()})
            except LLMError as e:
                self._send_json(502, {"error": str(e)})
            except Exception as e:  # noqa: BLE001
                self._send_json(500, {"error": str(e)})
        elif self.path == "/api/audio":
            self._send_json(200, {
                "current": audio.current(),
                "active": audio.describe()["active"],
                "input": audio.list_devices("input"),
                "output": audio.list_devices("output"),
            })
        elif self.path == "/api/profile":
            self._send_json(200, {
                "current": get_config().get("PROFILE", "") or "standard",
                "profiles": sorted(PROFILES),
            })
        elif self.path == "/api/settings":
            from config_ui import _settings_payload
            self._send_json(200, {"settings": _settings_payload()})
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

    def _origin_ok(self) -> bool:
        """Block cross-site (CSRF) POSTs: a browser always sends Origin on a
        cross-origin POST, even with mode:'no-cors'. Non-browser clients
        (urllib/http.client/tests) send none and are allowed."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        port = self.server.server_address[1]
        return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            self._send(403, b"forbidden origin", "text/plain")
            return
        if self.path == "/api/ask_voice":
            self._ask_voice()
            return
        if self.path in ("/api/llm", "/api/audio", "/api/settings") and not self._local_only():
            self._send_json(403, {"error": "config changes are local-only"})
            return
        if self.path == "/api/llm":
            self._set_llm()
            return
        if self.path == "/api/audio":
            self._set_audio()
            return
        if self.path == "/api/profile":
            self._set_profile()
            return
        if self.path == "/api/settings":
            self._set_settings()
            return
        if self.path != "/api/ask":
            self._send(404, b"not found", "text/plain")
            return
        self._ask()

    def _read_json(self) -> Optional[Dict[str, Any]]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(413 if length > MAX_BODY_BYTES else 400,
                            {"error": "bad body size"})
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send_json(400, {"error": "bad json"})
            return None

    def _ask(self) -> None:
        body = self._read_json()
        if body is None:
            return
        question = str(body.get("question", "")).strip()
        if not question:
            self._send_json(400, {"error": "empty question"})
            return
        if len(question) > MAX_QUESTION_CHARS:
            self._send_json(413, {"error": f"question too long (>{MAX_QUESTION_CHARS})"})
            return
        if not _ASK_SEMAPHORE.acquire(blocking=False):
            self._send_json(429, {"error": "busy, one question at a time"})
            return
        try:
            ctx = self.server.ctx  # type: ignore[attr-defined]
            eng: Engineer = ctx["engineer"]
            snap = ctx["state"].snapshot()
            answer = eng.ask(question, snap)
            usage = eng.last_usage
            try:
                ctx["recorder"].record_qa(question, answer, usage)
            except Exception:
                pass
            self._send_json(200, {"answer": answer, "error": eng.last_error,
                                  "usage": usage, "source": eng.last_source})
        finally:
            _ASK_SEMAPHORE.release()

    def _set_llm(self) -> None:
        body = self._read_json()
        if body is None:
            return
        cfg = get_config()
        for key in ("base_url", "api_key", "model"):
            if body.get(key):
                env_key = {"base_url": "LLM_BASE_URL", "api_key": "LLM_API_KEY",
                           "model": "LLM_MODEL"}[key]
                cfg.set_runtime(env_key, str(body[key]).strip())
        ctx = self.server.ctx  # type: ignore[attr-defined]
        ctx["engineer"].refresh_client()
        self._send_json(200, {"engineer": ctx["engineer"].describe()})

    def _set_audio(self) -> None:
        body = self._read_json()
        if body is None:
            return
        for kind, key in (("input", "input"), ("output", "output")):
            # An explicit empty string un-pins the device (follow the system
            # default); a non-empty string pins it by name fragment.
            if key in body and body[key] is not None:
                audio.set_device(kind, str(body[key]).strip())
        self._send_json(200, {"current": audio.current(),
                              "active": audio.describe()["active"]})

    def _set_profile(self) -> None:
        body = self._read_json()
        if body is None:
            return
        name = str(body.get("profile", "")).strip().lower()
        if name not in PROFILES:
            self._send_json(400, {"error": f"unknown profile: {name}",
                                  "profiles": sorted(PROFILES)})
            return
        get_config().set_runtime("PROFILE", name)
        self._send_json(200, {"current": name})

    def _set_settings(self) -> None:
        """POST /api/settings - batch feature toggles (validated + persisted)."""
        body = self._read_json()
        if body is None:
            return
        from config_ui import apply_settings
        self._send_json(200, apply_settings(body))

    def _ask_voice(self) -> None:
        """POST /api/ask_voice - raw audio body -> {question, answer, audio}."""
        ctx = self.server.ctx  # type: ignore[attr-defined]
        voice: VoiceLink = ctx["voice"]
        if not voice.available:
            self._send(503, json.dumps({"error": "voice unavailable"}).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_VOICE_BYTES:
            code = 413 if length > MAX_VOICE_BYTES else 400
            # Drain the request body (bounded) so the client can finish
            # uploading and still read the error response instead of dying
            # on a broken pipe.
            remaining = min(length, 64 * 1024 * 1024)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            self._send(code, json.dumps({"error": "bad audio size"}).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        audio = self.rfile.read(length)
        mime = (self.headers.get("Content-Type") or "audio/webm").split(";")[0].strip()
        try:
            result = voice.process(audio, mime)
        except Exception as e:  # noqa: BLE001 - answer the client, not a dropped conn
            logging.getLogger("f1_tr.web").warning("voice.process failed: %r", e)
            self._send(500, json.dumps({"error": f"voice failed: {e}"}).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        # Persist the Q&A turn exactly like /api/ask does.
        if result.get("question") is not None and result.get("answer") is not None:
            try:
                rec = ctx.get("recorder")
                if rec is not None:
                    eng = ctx.get("engineer")
                    usage = getattr(eng, "last_usage", None)
                    rec.record_qa(result["question"], result["answer"], usage)
            except Exception:
                pass
        self._send(200, json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")


def _receiver_thread(receiver: TelemetryReceiver, logger) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(receiver.run())
    except Exception as e:  # noqa: BLE001
        logger.error("receiver stopped: %r", e)


def serve(port: int = 20777, web_port: int = 8765,
          bind_ip: str = "127.0.0.1",
          udp_bind: Optional[str] = None,
          logger: Optional[logging.Logger] = None) -> None:
    logger = logger or logging.getLogger("f1_tr.web")
    # UDP bind is separate from the web-panel bind: a console/host player may
    # broadcast to the PC (needs 0.0.0.0) while the panel stays loopback.
    udp_bind = udp_bind or "127.0.0.1"
    app = build_app(port=port, bind_ip=udp_bind, logger=logger)
    voice = VoiceLink(app.engineer, app.state)
    app.extras["voice"] = voice

    t = threading.Thread(target=_receiver_thread,
                         args=(app.receiver, logger), daemon=True)
    t.start()
    # T3.8: run the ticker (race model + radio director) in web mode too; the
    # web path has no speech arbiter, so alerts go to the in-memory AlertLog.
    try:
        app.start_background()
    except Exception as e:  # noqa: BLE001
        logger.warning("background pipeline failed to start: %r", e)

    httpd = ThreadingHTTPServer((bind_ip, web_port), _Handler)
    httpd.ctx = app.ctx()  # type: ignore[attr-defined]
    httpd.bind_ip = bind_ip  # type: ignore[attr-defined]
    shown = "127.0.0.1" if bind_ip in ("0.0.0.0", "::") else bind_ip
    logger.info("web UI on http://%s:%s  (UDP %s)", bind_ip, web_port, port)
    print(f"\n>>> Open http://{shown}:{web_port}  "
          f"(UDP {udp_bind}:{port}, panel bind={bind_ip})", flush=True)
    if app.recorder is not None:
        print(f">>> Session recording to {app.recorder.path}", flush=True)
    if voice.available:
        tts_name = voice.tts.name if voice.tts_available else "off"
        print(f">>> Voice link: on (stt={voice.stt.name}, tts={tts_name})", flush=True)
    else:
        print(">>> Voice link: off (configure STT_* in .env to enable)", flush=True)
    print("", flush=True)
    httpd.serve_forever()
