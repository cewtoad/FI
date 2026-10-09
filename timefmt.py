"""Shared lap / gap display helpers (cheap unify for summariser + reports).

Keep these pure and dependency-free so summariser, debrief, and report_txt
never drift on how a lap time or gap is spelled for humans / the LLM.
"""

from __future__ import annotations

from typing import Optional


def fmt_ms(ms: Optional[int]) -> str:
    if ms is None or ms <= 0:
        return "-"
    minutes = ms // 60000
    rem = ms % 60000
    seconds = rem // 1000
    millis = rem % 1000
    if minutes:
        return f"{minutes}:{seconds:02d}.{millis:03d}"
    return f"{seconds}.{millis:03d}"


def fmt_signed_ms(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    return f"+{ms}ms" if ms >= 0 else f"{ms}ms"


def fmt_gap(ms: Optional[int]) -> str:
    if not ms or ms <= 0:
        return "-"
    return f"+{ms/1000:.3f}s"


def fmt_gap_signed(ms: Optional[int]) -> str:
    """Gap in ms with explicit direction for the player (落后 / 0.000s).

    F1 UDP deltas to leader / car ahead are non-negative when behind the
    reference. Spelling the direction out stops the LLM reading a positive
    gap as "how far I lead".
    """
    if not ms or ms <= 0:
        return "0.000s"
    return f"落后 {ms/1000:.3f}s"
