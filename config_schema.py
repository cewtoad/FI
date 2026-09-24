"""Single source of truth for configuration keys (T3.2).

Every runtime-tunable (and a few important static) option is declared here once
with its type, default, bounds/choices and UI metadata. ``config.RUNTIME_KEYS``
is derived from this schema, so adding a setting is a one-line change here.

Only keys with ``runtime=True`` may be changed via ``set_runtime`` / the config
page; the existing six keys MUST stay runtime (backward compatibility).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Tuple


@dataclass(frozen=True)
class Setting:
    key: str
    type: str                 # "str" | "int" | "float" | "bool" | "enum"
    default: Any
    group: str = "general"
    label: str = ""
    help: str = ""
    choices: Tuple[str, ...] = ()
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    # Runtime-mutable by default (the config page writes settings via
    # set_runtime). Set runtime=False for keys that must not change live.
    runtime: bool = True
    restart_required: bool = False
    secret: bool = False


SCHEMA: Tuple[Setting, ...] = (
    # ---- AI (existing runtime keys: keep) ----
    Setting("LLM_BASE_URL", "str", "", "ai", "LLM 地址", runtime=True),
    Setting("LLM_API_KEY", "str", "", "ai", "API Key", runtime=True, secret=True),
    Setting("LLM_MODEL", "str", "deepseek-flash", "ai", "模型", runtime=True),
    Setting("PROFILE", "enum", "standard", "ai", "回答档位",
            choices=("fast", "standard", "deep"), runtime=True),
    # ---- Audio devices (existing runtime keys: keep) ----
    Setting("AUDIO_INPUT", "str", "", "audio", "麦克风", runtime=True),
    Setting("AUDIO_OUTPUT", "str", "", "audio", "输出设备", runtime=True),
    # ---- Radio / proactive (T5 / §6.1) ----
    Setting("RADIO_ENABLE", "bool", True, "radio", "主动播报"),
    Setting("RADIO_VERBOSITY", "enum", "chatty", "radio", "话量",
            choices=("minimal", "normal", "chatty")),
    Setting("RADIO_GAP_MODE", "enum", "every_lap", "radio", "差距播报频率",
            choices=("off", "on_change", "every_lap", "every_n_laps")),
    Setting("RADIO_GAP_EVERY_N", "int", 3, "radio", "每 N 圈", min_value=1, max_value=50),
    Setting("RADIO_GATE_ENABLE", "bool", True, "radio", "直道时机闸门"),
    Setting("RADIO_GATE_MAX_WAIT_S", "int", 15, "radio", "闸门最长等待",
            min_value=1, max_value=120),
    Setting("RADIO_GATE_THROTTLE", "float", 0.9, "radio", "", min_value=0.0, max_value=1.0),
    Setting("RADIO_GATE_BRAKE", "float", 0.05, "radio", "", min_value=0.0, max_value=1.0),
    Setting("RADIO_GATE_STEER", "float", 0.15, "radio", "", min_value=0.0, max_value=1.0),
    Setting("RADIO_GATE_HOLD_S", "float", 0.5, "radio", "", min_value=0.0, max_value=5.0),
    Setting("RADIO_MIN_GAP_S", "float", 8.0, "radio", "最小间隔", min_value=0.0, max_value=120.0),
    Setting("RADIO_PER_LAP_CAP", "int", 6, "radio", "每圈上限", min_value=0, max_value=50),
    Setting("RADIO_QUIET_POLICY", "enum", "in_game", "radio", "安静模式策略",
            choices=("in_game", "force_on", "force_off")),
    Setting("RADIO_ALERT_RAIN_PCT", "int", 50, "radio", "降雨预警阈值",
            min_value=0, max_value=100),
    Setting("RADIO_BEEP", "bool", True, "radio", "提示音"),
    Setting("RADIO_FILTER", "bool", True, "radio", "无线电滤波"),
    Setting("DRIVER_NAME_STYLE", "enum", "zh", "radio", "车手名念法",
            choices=("zh", "en", "number")),
    # ---- Race model / tyres (§6.2) ----
    Setting("TYRE_WEAR_LIMIT_PCT", "float", 70.0, "model", "磨损阈值",
            min_value=1.0, max_value=100.0),
    Setting("TYRE_HOT_INNER_C", "float", 110.0, "model", "胎温报警阈值",
            min_value=40.0, max_value=250.0),
    Setting("PIT_WINDOW_WARN_LAPS", "int", 2, "model", "窗口预警提前量",
            min_value=0, max_value=20),
    # ---- PTT / TTS (§6.3) ----
    Setting("PTT_MODE", "enum", "toggle", "voice", "PTT 模式",
            choices=("hold", "toggle")),
    Setting("PTT_BINDING", "str", "kb:0x6B", "voice", "PTT 按键"),
    Setting("PTT_DOUBLE_TAP_WINDOW_MS", "int", 400, "voice", "双击窗口",
            min_value=100, max_value=1000),
    Setting("TTS_RATE", "int", 0, "voice", "语速", min_value=-10, max_value=10),
    Setting("TTS_VOLUME", "float", 1.0, "voice", "音量", min_value=0.0, max_value=2.0),
    Setting("TTS_VOICEPACK", "str", "", "voice", "语音包"),
    # ---- Debrief / config UI (§6.4) ----
    Setting("DEBRIEF_ENABLE", "bool", True, "debrief", "赛后复盘"),
    Setting("DEBRIEF_DIR", "str", "sessions", "debrief", "复盘输出目录"),
    Setting("CONFIG_UI_PORT", "int", 8766, "general", "配置页端口",
            min_value=1024, max_value=65535),
)


SCHEMA_BY_KEY = {s.key: s for s in SCHEMA}


def get_setting(key: str) -> Optional[Setting]:
    return SCHEMA_BY_KEY.get(key)


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
        except (TypeError, ValueError):
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
    return str(value)


def runtime_keys() -> frozenset:
    return frozenset(s.key for s in SCHEMA if s.runtime)
