"""Single source of truth for configuration keys (T3.2).

Every runtime-tunable (and a few important static) option is declared here once
with its type, default, bounds/choices and UI metadata. ``config.RUNTIME_KEYS``
is derived from this schema, so adding a setting is a one-line change here.

Only keys with ``runtime=True`` may be changed via ``set_runtime`` / the config
page; the existing six keys MUST stay runtime (backward compatibility).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class Setting:
    key: str
    type: str                 # "str" | "int" | "float" | "bool" | "enum"
    default: Any
    group: str = "general"
    label: str = ""           # Chinese label
    help: str = ""            # Chinese help (concrete: what it does / units)
    label_en: str = ""        # English label
    help_en: str = ""         # English help
    choices: Tuple[str, ...] = ()
    # Name of a dynamic option source (e.g. "voices" -> /api/voices) used by the
    # UI to render a dropdown instead of a free-text field.
    choices_from: str = ""
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    # Runtime-mutable by default (the config page writes settings via
    # set_runtime). Set runtime=False for keys that must not change live.
    runtime: bool = True
    restart_required: bool = False
    secret: bool = False


GROUP_LABELS = {
    "ai": ("AI / 模型", "AI / Model"),
    "audio": ("音频设备", "Audio devices"),
    "voice": ("语音 / 按键", "Voice / PTT"),
    "radio": ("主动播报", "Proactive radio"),
    "model": ("推演 / 轮胎", "Race model / Tyres"),
    "debrief": ("赛后复盘", "Debrief"),
    "general": ("通用", "General"),
}


SCHEMA: Tuple[Setting, ...] = (
    # ---- AI (existing runtime keys: keep) ----
    Setting("LLM_BASE_URL", "str", "", "ai", "LLM 接口地址", "OpenAI 兼容地址，如 https://api.deepseek.com；本地模型可填 http://localhost:11434/v1",
            "LLM endpoint", "OpenAI-compatible base URL, e.g. https://api.deepseek.com"),
    Setting("LLM_API_KEY", "str", "", "ai", "API 密钥", "服务商的 key；留空也能用本地快答与主动播报（不调用 AI）",
            "API key", "Provider key; leave empty to use local fast-answers only", secret=True),
    Setting("LLM_MODEL", "str", "deepseek-flash", "ai", "模型名", "需与上面的端点匹配，如 deepseek-flash",
            "Model", "Model id matching the endpoint, e.g. deepseek-flash"),
    Setting("PROFILE", "enum", "standard", "ai", "回答档位", "fast=一句话；standard=默认；deep=可给策略权衡（更长更慢）",
            "Answer profile", "fast=one line; standard=default; deep=strategy talk (longer)",
            choices=("fast", "standard", "deep")),
    Setting("LLM_TIMEOUT", "float", 30.0, "ai", "LLM 超时（秒）", "单次请求最长等待；超时会改用备用端点或报错",
            "LLM timeout (s)", "Max wait per request; on timeout it falls back or errors",
            min_value=1.0, max_value=300.0, restart_required=True),
    Setting("LLM_FALLBACK_BASE_URL", "str", "", "ai", "备用 LLM 地址", "主端点网络/超时/5xx 失败时自动改用（4xx 如 key 错不切换）",
            "Fallback endpoint", "Used on network/timeout/5xx; NOT on 4xx errors",
            restart_required=True),
    Setting("LLM_FALLBACK_API_KEY", "str", "", "ai", "备用 API 密钥", "备用端点的 key",
            "Fallback key", "Key for the fallback endpoint", restart_required=True, secret=True),
    Setting("LLM_FALLBACK_MODEL", "str", "", "ai", "备用模型", "备用端点的模型名",
            "Fallback model", "Model id for the fallback endpoint", restart_required=True),
    # ---- Audio devices (existing runtime keys: keep) ----
    Setting("AUDIO_INPUT", "str", "", "audio", "麦克风", "设备名片段；留空=跟随系统当前默认麦克风（拔插自动跟随）",
            "Microphone", "Device name fragment; empty = follow the system default mic"),
    Setting("AUDIO_OUTPUT", "str", "", "audio", "播报输出", "设备名片段；留空=跟随系统当前默认输出设备",
            "Speaker", "Device name fragment; empty = follow the system default output"),
    # ---- Radio / proactive (T5 / §6.1) ----
    Setting("RADIO_ENABLE", "bool", True, "radio", "主动播报", "总开关；关=只在被提问时回答，不主动出声",
            "Proactive radio", "Master switch; off = answer only when asked"),
    Setting("RADIO_VERBOSITY", "enum", "chatty", "radio", "话量档位", "minimal=仅安全+进站窗口；normal=加策略；chatty=全部（默认）",
            "Verbosity", "minimal=safety+pit only; normal=+strategy; chatty=all",
            choices=("minimal", "normal", "chatty")),
    Setting("RADIO_GAP_MODE", "enum", "every_lap", "radio", "差距播报频率", "off=不报；on_change=变化时；every_lap=每圈；every_n_laps=每 N 圈",
            "Gap report mode", "off / on_change / every_lap / every_n_laps",
            choices=("off", "on_change", "every_lap", "every_n_laps")),
    Setting("RADIO_GAP_EVERY_N", "int", 3, "radio", "差距每 N 圈", "仅当频率=every_n_laps 生效；≤0 视为不报",
            "Gap every N laps", "Only for every_n_laps mode; <=0 disables",
            min_value=1, max_value=50),
    Setting("RADIO_GATE_ENABLE", "bool", True, "radio", "直道时机闸门", "开=只在直道（油门≥阈值、无刹车、方向正）播非安全消息",
            "Straight-line gate", "Speak non-safety alerts only on straights"),
    Setting("RADIO_GATE_MAX_WAIT_S", "int", 15, "radio", "闸门最长等待（秒）", "排队消息等这么久还没到直道就丢弃，避免过时信息",
            "Gate max wait (s)", "Drop a queued alert if no straight within this time",
            min_value=1, max_value=120),
    Setting("RADIO_GATE_THROTTLE", "float", 0.9, "radio", "闸门·油门阈值", "判定直道：油门≥该值（0-1）",
            "Gate throttle", "Straight gate: throttle >= this (0-1)", min_value=0.0, max_value=1.0),
    Setting("RADIO_GATE_BRAKE", "float", 0.05, "radio", "闸门·刹车阈值", "判定直道：刹车≤该值（0-1）",
            "Gate brake", "Straight gate: brake <= this (0-1)", min_value=0.0, max_value=1.0),
    Setting("RADIO_GATE_STEER", "float", 0.15, "radio", "闸门·方向阈值", "判定直道：|方向盘|≤该值（0-1）",
            "Gate steer", "Straight gate: |steer| <= this (0-1)", min_value=0.0, max_value=1.0, ),
    Setting("RADIO_GATE_HOLD_S", "float", 0.5, "radio", "闸门·保持时间（秒）", "需连续满足直道条件的秒数才算“在直道”",
            "Gate hold (s)", "Must stay on-straight this long to count",
            min_value=0.0, max_value=5.0),
    Setting("RADIO_MIN_GAP_S", "float", 8.0, "radio", "最小间隔（秒）", "两条非安全播报之间的最短时间；安全播报不受限",
            "Min gap (s)", "Minimum spacing between non-safety alerts", min_value=0.0, max_value=120.0),
    Setting("RADIO_PER_LAP_CAP", "int", 6, "radio", "每圈上限（条）", "每圈最多播报条数（安全消息不计）",
            "Per-lap cap", "Max alerts per lap (safety excluded)", min_value=0, max_value=50),
    Setting("RADIO_QUIET_POLICY", "enum", "in_game", "radio", "安静模式策略", "in_game=局内可临时切；force_on/force_off=锁定（局内切换无效）",
            "Quiet policy", "in_game=can toggle; force_on/off=locked",
            choices=("in_game", "force_on", "force_off")),
    Setting("RADIO_ALERT_RAIN_PCT", "int", 50, "radio", "降雨预警阈值（%）", "预报降水概率≥该值且 10 分钟内将下雨才提醒",
            "Rain alert %", "Alert when forecast rain% >= this within 10 min",
            min_value=0, max_value=100),
    Setting("RADIO_FUEL_DEFICIT_LAPS", "float", -0.2, "radio", "缺油阈值（圈）", "预计剩余油量低于该圈数时预警（负数=缺油）",
            "Fuel deficit (laps)", "Warn when surplus laps < this (negative = short)",
            min_value=-5.0, max_value=0.0),
    Setting("RADIO_BEEP", "bool", True, "radio", "播报提示音", "播报前响一声“嘀”提示（无线电感）",
            "Radio beep", "Play a short beep before speaking"),
    Setting("RADIO_FILTER", "bool", True, "radio", "无线电滤波", "把播报做轻度带通+饱和，模拟车队电台音色",
            "Radio filter", "Light band-pass + saturation for a radio feel"),
    Setting("DRIVER_NAME_STYLE", "enum", "zh", "radio", "车手名念法", "zh=中文名；en=英文名；number=车号",
            "Driver name style", "zh / en / number",
            choices=("zh", "en", "number")),
    # ---- Race model / tyres (§6.2) ----
    Setting("TYRE_WEAR_LIMIT_PCT", "float", 70.0, "model", "轮胎磨损上限（%）", "磨损达到该值视为寿命用尽；用于推算“还能跑几圈”",
            "Tyre wear limit (%)", "Wear % treated as end-of-life for projections",
            min_value=1.0, max_value=100.0),
    Setting("TYRE_HOT_INNER_C", "float", 110.0, "model", "胎温报警阈值（℃）", "四轮内温中位数超过该值才报警（抗重刹尖峰）。默认110为占位，待实测标定",
            "Tyre hot threshold (C)", "Alert when inner-temp median exceeds this. 110 is a placeholder",
            min_value=40.0, max_value=250.0),
    Setting("PIT_WINDOW_WARN_LAPS", "int", 2, "model", "进站窗口预警（圈）", "距窗口打开还有几圈时提前提示",
            "Pit window warn (laps)", "Warn N laps before the window opens",
            min_value=0, max_value=20),
    # ---- PTT / TTS (§6.3) ----
    Setting("PTT_MODE", "enum", "toggle", "voice", "PTT 模式", "hold=按住说话；toggle=按一下开始、再按一下结束",
            "PTT mode", "hold=push to talk; toggle=tap to start/stop",
            choices=("hold", "toggle")),
    Setting("PTT_BINDING", "str", "kb:0x6B", "voice", "PTT 按键", "kb:0x6B（小键盘+）或 hid:VID:PID:byte:mask（手柄，可在网页一键捕获；保存后重启语音模式生效）",
            "PTT binding", "kb:<vk> or hid:VID:PID:byte:mask (gamepad; one-click capture in the web panel, restart voice mode after saving)"),
    Setting("PTT_DOUBLE_TAP_WINDOW_MS", "int", 400, "voice", "双击窗口（已停用）", "保留以兼容旧 .env；局内双击静音已取消（静音改到本页设置）",
            "Double-tap window (disabled)", "Kept for old .env; in-game double-tap quiet removed",
            min_value=100, max_value=1000),
    Setting("STT_PROVIDER", "enum", "auto", "voice", "语音识别方式", "local=本地识别（离线免费）；cloud=云端 API；auto=填了 key 用云端、否则本地；off=关闭",
            "STT provider", "local=offline; cloud=API; auto=key decides; off",
            choices=("local", "cloud", "auto", "off")),
    Setting("STT_LOCAL_ENGINE", "enum", "sensevoice", "voice", "本地识别引擎", "sensevoice=SenseVoice-Small（快 6-10 倍、中文更准，需下载模型）；whisper=faster-whisper；sensevoice 未安装时自动回退 whisper",
            "Local STT engine", "sensevoice (fast, needs model download) or whisper; auto-falls back",
            choices=("sensevoice", "whisper")),
    Setting("STT_LOCAL_MODEL", "enum", "small", "voice", "本地识别模型", "越小越快：tiny 最快 / base 快且更准 / small 默认 / medium 慢（CPU）",
            "Local STT model", "tiny/base/small/medium — smaller is faster on CPU",
            choices=("tiny", "base", "small", "medium")),
    Setting("STT_LOCAL_THREADS", "int", 2, "voice", "本地识别线程", "2-3 最稳；调太高会和游戏抢 CPU",
            "Local STT threads", "2-3 recommended; too high starves the game",
            min_value=1, max_value=8),
    Setting("STT_API_KEY", "str", "", "voice", "识别 API 密钥", "云端识别用（如硅基流动）；本地识别不需要",
            "STT API key", "for cloud STT (e.g. SiliconFlow); not needed for local",
            secret=True),
    Setting("STT_BASE_URL", "str", "", "voice", "识别接口地址", "OpenAI 兼容地址，如 https://api.siliconflow.cn/v1",
            "STT base URL", "OpenAI-compatible, e.g. https://api.siliconflow.cn/v1"),
    Setting("STT_MODEL", "str", "whisper-1", "voice", "识别模型名", "云端模型名：硅基流动填 SenseVoiceSmall，OpenAI 填 whisper-1",
            "STT model", "SiliconFlow: SenseVoiceSmall; OpenAI: whisper-1"),
    Setting("TTS_RATE", "int", 0, "voice", "语速", "-10..10，0=正常；各 TTS 引擎自动换算",
            "Speech rate", "-10..10, 0=normal (auto-converted per engine)",
            min_value=-10, max_value=10),
    Setting("TTS_VOLUME", "float", 1.0, "voice", "音量", "0.0-2.0，1.0=原始音量",
            "Volume", "0.0-2.0, 1.0=original",
            min_value=0.0, max_value=2.0),
    Setting("TTS_VOICEPACK", "str", "", "voice", "语音包", "从下方列表选一个发声人（留空=按 TTS_PROVIDER 自动：有 Piper 用 Piper，否则 SAPI）",
            "Voice", "Pick a voice from the list (empty = auto by TTS_PROVIDER)",
            choices_from="voices"),
    Setting("TTS_PIPER_VOICE", "str", "", "voice", "Piper 模型路径", "本地 .onnx 路径；用 Piper 播报时必填（完全离线）",
            "Piper model path", "Local .onnx path; required for Piper (fully offline)"),
    # ---- Debrief / config UI (§6.4) ----
    Setting("DEBRIEF_ENABLE", "bool", True, "debrief", "赛后复盘", "过终点/会话结束时自动写一份 TXT 复盘到输出目录",
            "Post-race debrief", "Write a TXT debrief at session end"),
    Setting("DEBRIEF_DIR", "str", "sessions", "debrief", "复盘输出目录", "相对程序目录；默认 sessions/",
            "Debrief folder", "Relative to the app folder; default sessions/"),
    Setting("CONFIG_UI_PORT", "int", 8766, "general", "配置页端口", "FI.py --config 的监听端口（改后需重启设置页）",
            "Config page port", "Port for FI.py --config (restart the config page)",
            min_value=1024, max_value=65535),
)


SCHEMA_BY_KEY = {s.key: s for s in SCHEMA}


def get_setting(key: str) -> Optional[Setting]:
    return SCHEMA_BY_KEY.get(key)


# PTT_BINDING: kb:<vk> | hid:VID:PID:byte:mask | hat:VID:PID:byte:value —
# matches inputs.bindings.parse_binding semantics: VID/PID always hex (0x
# optional), byte/mask/value are int(x, 0); hat value must be 0-7 (8=neutral
# would mean "always pressed").
_BINDING_RE = re.compile(
    r"^kb:(0x[0-9a-fA-F]+|\d+)$"
    r"|^hid:[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}"
    r":(0x[0-9a-fA-F]{1,2}|\d+):(0x[0-9a-fA-F]{1,2}|\d+)$"
    r"|^hat:[0-9a-fA-F]{1,4}:[0-9a-fA-F]{1,4}"
    r":(0x[0-9a-fA-F]{1,2}|\d+):[0-7]$")


def validate(key: str, value: Any) -> Any:
    """Coerce/validate a value against the schema; raise ValueError if invalid.

    Returns the coerced Python value (bool/int/float/str).
    """
    s = SCHEMA_BY_KEY.get(key)
    if s is None:
        raise ValueError(f"unknown setting: {key}")
    if s.type == "bool":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off", ""):
            return False
        raise ValueError(f"{key}: not a boolean: {value!r}")
    if s.type == "int":
        try:
            iv = int(float(value))
        except (TypeError, ValueError, OverflowError):
            # OverflowError: int(float("inf")) is not a ValueError.
            raise ValueError(f"{key}: not an integer: {value!r}")
        if s.min_value is not None and iv < s.min_value:
            raise ValueError(f"{key}: below minimum {s.min_value}")
        if s.max_value is not None and iv > s.max_value:
            raise ValueError(f"{key}: above maximum {s.max_value}")
        return iv
    if s.type == "float":
        try:
            fv = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key}: not a number: {value!r}")
        # NaN slips past every min/max comparison; reject non-finite values
        # so a typo like "nan" cannot poison numeric settings.
        if not math.isfinite(fv):
            raise ValueError(f"{key}: not a finite number: {value!r}")
        if s.min_value is not None and fv < s.min_value:
            raise ValueError(f"{key}: below minimum {s.min_value}")
        if s.max_value is not None and fv > s.max_value:
            raise ValueError(f"{key}: above maximum {s.max_value}")
        return fv
    if s.type == "enum":
        text = str(value).strip().lower()
        if text not in s.choices:
            raise ValueError(f"{key}: must be one of {s.choices}")
        return text
    if key == "PTT_BINDING":
        text = str(value).strip()
        if not _BINDING_RE.match(text):
            raise ValueError(
                f"{key}: expected kb:<vk>, hid:VID:PID:byte:mask or "
                f"hat:VID:PID:byte:value, got {value!r}")
        return text
    text = str(value)
    if "\n" in text or "\r" in text:
        # A newline inside a value would inject extra keys into .env.
        raise ValueError(f"{key}: newlines are not allowed in setting values")
    # Strip remaining control characters that would corrupt the .env line.
    return "".join(ch for ch in text if ord(ch) >= 32 or ch == "\t")


def runtime_keys() -> frozenset:
    return frozenset(s.key for s in SCHEMA if s.runtime)
