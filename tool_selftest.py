"""Whole-app self check for 整体测试.bat.

Runs a quick pass over every subsystem and prints a PASS/FAIL summary:
    Python / paths / config / LLM key / telemetry parse / state+summariser /
    STT deps & model / TTS synth / audio devices.

Usage:
    py -3.12 tool_selftest.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

results: list[tuple[str, bool, str]] = []


def check(name: str):
    def deco(fn):
        try:
            detail = fn() or ""
            results.append((name, True, detail))
        except Exception as e:  # noqa: BLE001
            results.append((name, False, f"{type(e).__name__}: {e}"))
        return fn
    return deco


@check("Python 版本")
def _py():
    return f"{sys.version.split()[0]} ({'embedded' if not (ROOT / '.git').exists() else 'source'})"


@check("路径锚点 app_root()")
def _paths():
    from paths import app_root
    return str(app_root())


@check("配置 config.get")
def _config():
    from config import get_config
    m = get_config().get("LLM_MODEL") or get_config().get("DEEPSEEK_MODEL") or "(默认)"
    return f"LLM_MODEL={m}"


@check("LLM key 已配置")
def _llm_key():
    from config import get_config
    k = get_config().get("LLM_API_KEY") or get_config().get("DEEPSEEK_API_KEY")
    if not k or k == "sk-your-key-here":
        raise RuntimeError("未配置（AI 问答不可用，但本地快答可用）")
    return f"key=***{k[-4:]}"


@check("遥测解析（17 种 packet 导入）")
def _parse():
    from lib.f1_types import F1PacketType
    return f"{len(list(F1PacketType))} 种"


@check("状态层 + 总结层")
def _state():
    from state import TelemetryState
    from summariser import Summariser
    snap = TelemetryState().snapshot()
    s = Summariser().summarise(snap)
    return f"facts={len(s['facts'])} notes={len(s['notes'])}"


@check("语音识别依赖 (faster-whisper)")
def _stt_deps():
    from stt_client import LocalWhisperSTT
    e = LocalWhisperSTT()
    if not e.available:
        raise RuntimeError("未安装 faster-whisper（可用云端 STT 或全量包）")
    return "可用"


@check("语音模型加载（CPU int8，离线）")
def _stt_model():
    from stt_client import LocalWhisperSTT
    from paths import app_root
    e = LocalWhisperSTT()
    if not e.available:
        raise RuntimeError("跳过：无 faster-whisper")
    if not (app_root() / "stt_models").exists():
        raise RuntimeError("跳过：无 stt_models/ 目录")
    e.load()
    return f"模型 {e.model_size} 就绪"


@check("语音播报合成 (SAPI)")
def _tts():
    from tts_client import make_tts
    e = make_tts()
    if e is None:
        raise RuntimeError("无可用 TTS")
    b = e.synthesize("测试")
    return f"{e.name}, {len(b)} bytes"


@check("音频设备解析")
def _audio():
    import audio
    d = audio.describe()
    i = d["active"]["input"]
    o = d["active"]["output"]
    return (f"麦克风={i['name'] or '无'}（{i['source']}）"
            f" 播放={o['name'] or '无'}（{o['source']}）")


@check("资源文件（data/ 等只读资源）")
def _resources():
    from paths import resource_root
    root = resource_root()
    missing = []
    for rel in ("data/driver_names.json",):
        if not (root / rel).exists():
            missing.append(rel)
    if missing:
        raise RuntimeError(f"缺少: {', '.join(missing)}")
    return f"resource_root={root}"


@check("UDP 端口预检 (20777)")
def _port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("127.0.0.1", 20777))
        except OSError:
            raise RuntimeError("20777 已被占用（SimHub/CrewChief？游戏数据收不到）")
    return "可用"


@check(".env 可读")
def _env_readable():
    from config import get_config
    cfg = get_config()
    model = cfg.get("LLM_MODEL") or "(默认)"
    return f"LLM_MODEL={model}, 可写={cfg.is_writable()}"


@check("SAPI 中文音色")
def _sapi_voice():
    import sys
    if sys.platform != "win32":
        raise RuntimeError("跳过：非 Windows")
    from voices import list_voices
    sapi = [v for v in list_voices() if v.provider == "sapi"]
    if not sapi:
        raise RuntimeError("未找到 SAPI 音色（Windows 未装语音包？）")
    # Chinese SAPI voices often have romanized names (Huihui/Yaoyao/Kangkang…),
    # so match on a known-name list in addition to CJK characters / "Chinese".
    zh_names = ("huihui", "yaoyao", "kangkang", "lili", "chinese", "zh-")
    zh = [v for v in sapi
          if any("\u4e00" <= ch <= "\u9fff" for ch in v.voice)
          or any(n in v.voice.lower() for n in zh_names)]
    return f"SAPI={len(sapi)} 个，中文={len(zh)} 个" + (
        f"（{', '.join(v.voice for v in zh)}）" if zh else "")


@check("统一音频出口 (AudioPlayer 解码)")
def _audio_player():
    from speech import AudioPlayer, UnsupportedAudioFormat
    import radio_fx
    if not radio_fx.has_numpy():
        raise RuntimeError("numpy 不可用（滤波器/提示音将跳过）")
    beep = radio_fx.make_beep()
    if not beep:
        raise RuntimeError("提示音合成失败")
    p = AudioPlayer()
    try:
        p._decode(beep, "audio/wav")
    except UnsupportedAudioFormat as e:
        raise RuntimeError(str(e))
    return f"beep={len(beep)}B, wav 解码 OK"


@check("组装根 build_app（电台/推演）")
def _pipeline():
    from app import build_app
    application = build_app(port=0, recording=False, mode="web", logger=None)
    missing = []
    if application.race_model is None:
        missing.append("race_model")
    if application.radio is None:
        missing.append("radio")
    if application.ticker is None:
        missing.append("ticker")
    try:
        application.shutdown()
    except Exception:
        pass
    if missing:
        raise RuntimeError("缺失: " + ", ".join(missing))
    return "race_model+radio+ticker 就绪"



def main() -> int:
    print("=" * 52)
    print("  F1 Race Engineer  整体自检")
    print("=" * 52)
    fails = 0
    for name, ok, detail in results:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}: {detail}")
        if not ok:
            fails += 1
    print("-" * 52)
    print(f"结果: {len(results) - fails}/{len(results)} 通过")
    if fails:
        print("提示: FAIL 项不一定致命（如未配 key / 无本地语音），见上方说明")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
