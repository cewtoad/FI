"""Cross-layer data contracts (T3.1).

These dataclasses are the typed boundary between the layers that grew in v2:
the race model (推演层), the radio director (主动播报), the speech arbiter and
the output sinks. Keeping them here (rather than passing bare dicts) makes the
interfaces explicit and testable.

All are plain dataclasses with no behaviour and no project imports, so they can
be imported from anywhere without cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


# --------------------------------------------------------------------- alerts

# Priorities: 0 = P0 safety, 1 = P1 strategy, 2 = P2 info. Answer utterances use
# 0.5 (between P0 and P1) so they jump ahead of strategy chatter.
PRIORITY_P0 = 0
PRIORITY_ANSWER = 0.5
PRIORITY_P1 = 1
PRIORITY_P2 = 2


@dataclass(frozen=True)
class Alert:
    """A one-shot proactive message produced by a radio rule."""

    id: str
    category: str
    priority: int
    text: str
    created_at: float
    dedup_key: str = ""
    session_kinds: Tuple[str, ...] = ()
    meta: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- utterances

@dataclass(frozen=True)
class Utterance:
    """Something to be spoken by the SpeechArbiter (single audio outlet)."""

    text: str
    priority: float
    source: str            # "rule" | "answer" | "system"
    created_at: float
    dedup_key: str = ""
    gated: bool = True     # False = bypass the straight-line gate (P0/answers)
    interruptible: bool = True


# -------------------------------------------------------------- race model

@dataclass
class Stint:
    index: int
    start_lap: int
    compound: str
    laps: int
    wear_rate_pct_per_lap: float
    wear_now_pct: float
    pace_degradation_s_per_lap: float
    projected_life_laps: Optional[float]   # None = no measurable wear yet


@dataclass
class GapTrend:
    target_index: int
    gap_ms_now: int
    closing_rate_ms_per_lap: float
    laps_to_1s: Optional[float]


@dataclass
class PitWindow:
    state: str             # not_open | open | last_lap | missed | done | unknown
    ideal_lap: int
    latest_lap: int
    rejoin_position: int


@dataclass
class RaceModelState:
    upto_lap: int
    session_kind: str
    stint: Optional[Stint] = None
    ahead: Optional[GapTrend] = None
    behind: Optional[GapTrend] = None
    pit_window: Optional[PitWindow] = None
    tyre_laps_to_limit: Optional[float] = None
    fuel_laps_left: Optional[float] = None
    rain_eta_min: Optional[float] = None
    field_best_lap_ms: Optional[int] = None
    pole_lap_ms: Optional[int] = None
    flags: Dict[str, Any] = field(default_factory=dict)


# -------------------------------------------------------------- voice packs

@dataclass(frozen=True)
class VoicePack:
    """A selectable voice: provider + engine-specific voice id + tuning."""

    id: str
    provider: str          # "sapi" | "piper"
    voice: str
    rate: str = ""
    volume: str = ""
    label: str = ""
