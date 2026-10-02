"""Text normalization for TTS — the last step before audio synthesis.

Chinese TTS engines read telemetry shorthand badly: "1:31.204" as a weird
digit run, "P5" as English "P five", trailing "C"/"s" spelled out, "98/101/97"
as "九十八斜杠..." Applied ONLY at the synthesis moment (speech.py
_do_synthesize): queue contents, logs, alerts and session reports keep the
original text. Pure + unit-tested.
"""

from __future__ import annotations

import re

_CN_DIGITS = "零一二三四五六七八九"


def _cn_num(s: str) -> str:
    """Arabic numerals -> concise Chinese reading (5->五, 12->十二, 20->二十).

    >=100 is left as digits: TTS reads long numbers fine in context.
    """
    n = int(s)
    if n == 0:
        return "零"
    if n < 10:
        return _CN_DIGITS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN_DIGITS[n % 10]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _CN_DIGITS[tens] + "十" + (_CN_DIGITS[ones] if ones else "")
    return s


# NOTE: \b is WRONG here — Python re counts CJK as word chars, so "掉到P12" /
# "胎温97C" (no space) would never match. Use explicit lookarounds that only
# exclude ASCII alphanumerics; CJK adjacency must still match.

# 1:31.204 -> 1分31秒204 (lap times; H:MM:SS without .mmm is NOT matched)
_LAP_TIME = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})\.(\d{1,3})(?![\d.])")
# 2.1s / 19.982s / 8s -> ...秒 (gaps); "DRS"/"undercut" letters blocked
_GAP_S = re.compile(r"(?<![\dA-Za-z])(\d+(?:\.\d+)?)s(?![A-Za-z0-9])")
# 97C / 97 C -> ...度 (tyre temps)
_TEMP_C = re.compile(r"(?<![\dA-Za-z])(\d+(?:\.\d+)?)\s*C(?![A-Za-z0-9])")
# P5 / P12 -> P五 / P十二 (but not "WP5")
_POS = re.compile(r"(?<![A-Za-z0-9])P(\d{1,2})(?!\d)")
# 30% -> 百分之30
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
# "/" between numbers -> 、
_SLASH = re.compile(r"(?<=\d)\s*/\s*(?=\d)")


def normalize_for_tts(text: str) -> str:
    """Convert telemetry shorthand to speakable Chinese. Idempotent enough to
    be safe on already-normalized text; never raises on odd input."""
    if not text:
        return text
    out = _LAP_TIME.sub(lambda m: f"{m.group(1)}分{m.group(2)}秒{m.group(3)}", text)
    out = _GAP_S.sub(lambda m: f"{m.group(1)}秒", out)
    out = _TEMP_C.sub(lambda m: f"{m.group(1)}度", out)
    out = _POS.sub(lambda m: "P" + _cn_num(m.group(1)), out)
    out = _PERCENT.sub(lambda m: f"百分之{m.group(1)}", out)
    out = _SLASH.sub("、", out)
    return out
