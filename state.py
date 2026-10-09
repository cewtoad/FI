"""Lightweight telemetry state for a single player car.

This is our own minimal replacement for pits-n-giggles' heavy SessionState.
It only keeps what the TR layer and the summariser need:

    - latest per-packet values for the player's car
    - a rolling history of a few derived quantities
    - a LapDeltaManager to answer "am I faster/slower than my best lap"
    - a FuelRateRecommender to answer fuel questions

No config, no IPC, no async event bus, no network. Plain CPU-bound aggregation.
"""

from __future__ import annotations

import copy
import statistics
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional

from lib.delta import LapDeltaManager
from lib.f1_types import (F1PacketType, F1Utils, LapHistoryData,
                          PacketEventData)
from lib.fuel_rate_recommender import FuelRateRecommender, FuelRemainingPerLap
from lib.rolling_history import RollingHistory


class TelemetryState:
    """Aggregates parsed packets for the player car into a queryable snapshot."""

    # How many laps of history to keep for trend answers. T1.4: raised from 20
    # to 100 (a full race is up to ~70 laps; memory is trivial).
    HISTORY_LAPS = 100
    # Minimum fuel required to finish (safety margin, kg). Adjust later via config.
    MIN_FUEL_KG = 1.5
    # After a player-involved official OVERTAKE event, a leaderboard position
    # diff in the same direction within this window is treated as the same
    # overtake (already reported) and not recorded again.
    OFFICIAL_EVENT_DEDUP_S = 8.0
    # T1.3: window (seconds) over which the tyre inner-temperature median is
    # taken. A brake-zone spike is short; the median filters it out.
    TYRE_TEMP_MEDIAN_WINDOW_S = 3.0

    def __init__(self, error_logger: Optional[Any] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._error_logger = error_logger
        # Injectable monotonic clock (R5): tests drive time deterministically.
        self._clock = clock
        self.session_uid: Optional[int] = None
        self.session_started: bool = False

        # Identity / session meta
        self.session_type: Optional[str] = None
        self.session_kind: Optional[str] = None
        self.track_id: Optional[Any] = None
        self.total_laps: Optional[int] = None
        self.player_car_index: int = 0

        # Latest snapshot values (player car only)
        self.latest: Dict[str, Any] = {}
        self.packet_counts: Dict[str, int] = {}
        self.packet_errors: Dict[str, int] = {}
        # T1.3: rolling (ts, inner_temp_c list) samples for the tyre median.
        self._tyre_inner_samples: deque = deque()
        # T5.7: monotonic timestamp when the car last met the "straight"
        # condition (throttle/brake/steer); None while not on a straight.
        self._straight_since: Optional[float] = None

        # Trend histories (per-lap samples)
        self.lap_times_ms: RollingHistory = RollingHistory(self.HISTORY_LAPS)
        self.fuel_per_lap: RollingHistory = RollingHistory(self.HISTORY_LAPS)
        self.tyre_wear_per_lap: RollingHistory = RollingHistory(self.HISTORY_LAPS)
        # Full completed-lap records (valid AND invalid), each entry
        # {"lap_num", "lap_time_ms", "valid"}. Invalid laps are kept here for
        # the lap history display, but never enter lap_times_ms / best-lap.
        self.lap_records: RollingHistory = RollingHistory(self.HISTORY_LAPS)

        # Analyzers
        self.delta = LapDeltaManager()
        self.fuel: Optional[FuelRateRecommender] = None

        # Internal bookkeeping to detect lap boundaries
        self._last_current_lap_num: Optional[int] = None
        # m_currentLapInvalid as last seen on the lap still being driven; the
        # validity of the lap that just ended is read from here at the boundary.
        self._last_lap_invalid: bool = False
        self._last_fuel_in_tank: Optional[float] = None
        self._best_lap_ms: Optional[int] = None
        # Fuel bookkeeping: lap_num -> fuel at end of that lap
        self._fuel_at_lap_end: Dict[int, float] = {}
        self._fuel_recorded: set = set()
        self._fuel_lap_num: Optional[int] = None

        # Full-field data (all cars) for the leaderboard.
        self._all_lap_data: Optional[list] = None
        self._participants: Dict[int, Dict[str, Any]] = {}   # car_idx -> info
        self._all_car_status: Optional[list] = None
        self.leaderboard: list = []
        # T2.3: best valid lap time (ms) seen per car index. Approximate: the
        # per-car validity flag is the current-lap flag, not a per-lap record,
        # so this tracks the min of non-zero last-lap times only.
        self._best_lap_by_car: Dict[int, int] = {}

        # Event tracking (position changes etc.) - fed to the AI as "what just
        # happened" so it can answer overtake questions with authority.
        self._last_position: Optional[int] = None
        self.events: list = []          # recent notable events, newest last
        self._event_seq: int = 0
        self._event_session_time: Optional[float] = None
        # T2.5: set by FLBK, consumed by the next LAP_DATA to roll back derived
        # lap-based data (lap_records/best-lap/fuel trends/recommender).
        self._pending_flashback: bool = False
        # T4.1 (declared here so flashback reset is centralised): full per-lap
        # snapshot samples, kept as a plain list for easy rewinding.
        self.lap_snapshots: List[Dict[str, Any]] = []

        # Spectator mode: when spectating, the car we care about is the one
        # being watched (spectatorCarIndex), not playerCarIndex.
        self.is_spectating: bool = False
        self.spectator_car_index: Optional[int] = None

        # Dedup between official OVERTAKE events and leaderboard position
        # diffs: ("up"|"down", expiry monotonic time) set when an official
        # player-involved overtake is recorded, consumed by the next diff.
        self._pending_diff_event: Optional[str] = None
        self._pending_diff_until: float = 0.0

        # Frozen snapshot: the receiver thread builds an immutable deep copy at
        # most SNAPSHOT_HZ times a second; every consumer reads that frozen copy
        # under a lock instead of the live (mutating) structures. This removes
        # the reader/writer races in the web and voice paths.
        self._snap_lock = threading.Lock()
        # Serialises writers (process) with snapshot builds. snapshot() on a
        # reader thread rebuilds from the LIVE structures whenever the state is
        # dirty (i.e. almost always while packets flow), so without this lock
        # readers deep-copy dicts/deques the RX thread is mutating.
        self._state_lock = threading.RLock()
        # Single-flight for reader-triggered rebuilds: N concurrent readers
        # behind a stale snapshot must produce ONE rebuild, not N (each one
        # deep-copies everything under _state_lock and stalls the RX loop).
        self._build_lock = threading.Lock()
        # Cached straight-gate thresholds (see _straight_thresholds).
        self._straight_cfg: Optional[tuple] = None
        self._frozen: Dict[str, Any] = {}
        self._frozen_at: float = 0.0
        self._snap_dirty: bool = True
        # T3.8: extra snapshot providers (e.g. the race model). Each returns a
        # dict merged under its own key into every built snapshot.
        self._snapshot_providers: List[Callable[[], Dict[str, Any]]] = []

    def add_snapshot_provider(self, fn: Callable[[], Dict[str, Any]]) -> None:
        """Register a provider merged into every snapshot (T3.8).

        Providers run inside snapshot builds, i.e. while ``_state_lock`` is
        held on the receiving thread's path: they must be pure in-memory reads
        of their own (internally locked) structures — no IO, no callbacks into
        TelemetryState, no blocking — or the receive loop stalls.
        """
        self._snapshot_providers.append(fn)

    # ---------------------------------------------------------------- session

    def note_header(self, header) -> bool:
        """Update session identity from a packet header.

        Returns True if a new session was detected (uid changed), which is our
        signal that a race/session just started (or restarted).
        """
        uid = header.m_sessionUID
        is_new = self.session_uid is not None and uid != self.session_uid
        self.session_uid = uid
        self.session_started = True
        if is_new:
            self._reset_for_new_session()
        return is_new

    def _reset_for_new_session(self) -> None:
        self.latest.clear()
        self.packet_counts.clear()
        self.packet_errors.clear()
        self.session_type = None
        self.session_kind = None
        self.lap_times_ms.clear()
        self.fuel_per_lap.clear()
        self.tyre_wear_per_lap.clear()
        self.lap_records.clear()
        self.delta = LapDeltaManager()
        self.fuel = None
        self._last_current_lap_num = None
        self._last_lap_invalid = False
        self._last_fuel_in_tank = None
        self._best_lap_ms = None
        self._fuel_at_lap_end = {}
        self._fuel_recorded = set()
        self._fuel_lap_num = None
        self._all_lap_data = None
        self._tyre_inner_samples = deque()
        self._straight_since = None
        self._participants = {}
        self._all_car_status = None
        self.leaderboard = []
        self._best_lap_by_car = {}
        self._last_position = None
        self.events = []
        self._event_seq = 0
        self._event_session_time = None
        self._pending_flashback = False
        self.lap_snapshots = []
        self.is_spectating = False
        self.spectator_car_index = None
        self._pending_diff_event = None
        self._pending_diff_until = 0.0

    # --------------------------------------------------------------- packets

    def process(self, packet) -> None:
        """Dispatch a parsed packet object into the state.

        Any single-packet failure is contained: recorded and dropped, never
        allowed to escape and kill the receive loop. F1 updates can ship new
        fields/enums that older handlers don't expect.
        """
        with self._state_lock:
            try:
                self._dispatch(packet)
            except Exception as e:  # noqa: BLE001 - intentionally broad
                pid = getattr(getattr(packet, "m_header", None), "m_packetId", "?")
                key = f"__ERROR__{pid}"
                self.packet_errors[key] = self.packet_errors.get(key, 0) + 1
                if self._error_logger is not None:
                    self._error_logger.warning("state.process failed for %s: %r", pid, e)
            finally:
                # Live state changed: the frozen snapshot is now stale. The
                # receiver thread rebuilds it (throttled); direct callers get a
                # fresh build on the next snapshot().
                self.mark_dirty()

    def _dispatch(self, packet) -> None:
        header = packet.m_header
        self.note_header(header)
        # T2.4: remember the session clock so events can carry a timestamp.
        self._event_session_time = getattr(header, "m_sessionTime", None)
        # The "focus car" is the one we report on: normally the player's own
        # car, but in spectator mode it's the car being watched.
        self.player_car_index = self._resolve_focus_car(header)

        pid = header.m_packetId
        name = str(pid)
        self.packet_counts[name] = self.packet_counts.get(name, 0) + 1

        if pid == F1PacketType.SESSION:
            self._on_session(packet)
        elif pid == F1PacketType.LAP_DATA:
            self._on_lap_data(packet)
        elif pid == F1PacketType.CAR_TELEMETRY:
            self._on_car_telemetry(packet)
        elif pid == F1PacketType.CAR_STATUS:
            self._on_car_status(packet)
        elif pid == F1PacketType.CAR_DAMAGE:
            self._on_car_damage(packet)
        elif pid == F1PacketType.CAR_TELEMETRY_2:
            self._on_car_telemetry_2(packet)
        elif pid == F1PacketType.SESSION_HISTORY:
            self._on_session_history(packet)
        elif pid == F1PacketType.TIME_TRIAL:
            self._on_time_trial(packet)
        elif pid == F1PacketType.PARTICIPANTS:
            self._on_participants(packet)
        elif pid == F1PacketType.EVENT:
            self._on_event(packet)
        elif pid == F1PacketType.FINAL_CLASSIFICATION:
            self._on_final_classification(packet)

    def _resolve_focus_car(self, header) -> int:
        """Choose which car index to report on.

        Normally the player's own car (header.m_playerCarIndex), but while
        spectating it's the car being watched. We only trust
        spectatorCarIndex once a Session packet has told us we're spectating.
        """
        if self.is_spectating and self.spectator_car_index is not None:
            idx = self.spectator_car_index
            if idx != F1Utils.PLAYER_INDEX_INVALID:
                return idx
        return header.m_playerCarIndex

    def _player(self, arr: List[Any]) -> Optional[Any]:
        if arr is None:
            return None
        idx = self.player_car_index
        if 0 <= idx < len(arr):
            return arr[idx]
        return None

    def _on_session(self, packet) -> None:
        self.session_type = str(packet.m_sessionType)
        self.track_id = packet.m_trackId
        self.total_laps = packet.m_totalLaps
        # Track spectator state so we report on the car being watched.
        self.is_spectating = bool(getattr(packet, "m_isSpectating", False))
        sci = getattr(packet, "m_spectatorCarIndex", None)
        self.spectator_car_index = sci if isinstance(sci, int) else None

        weather = packet.m_weather if hasattr(packet, "m_weather") else None
        session_kind = self._session_kind(packet.m_sessionType)
        self.session_kind = session_kind
        self.latest["session"] = {
            "session_type": str(packet.m_sessionType),
            # T2.1: coarse kind for rule-set selection (practice/qualifying/
            # race/time_trial); keeps the legacy string session_type intact.
            "session_kind": session_kind,
            "track_id": str(packet.m_trackId),
            "track_length_m": getattr(packet, "m_trackLength", None),
            "total_laps": getattr(packet, "m_totalLaps", None),
            "session_time_left_s": getattr(packet, "m_sessionTimeLeft", None),
            "session_duration_s": getattr(packet, "m_sessionDuration", None),
            "air_temp_c": getattr(packet, "m_airTemperature", None),
            "track_temp_c": getattr(packet, "m_trackTemperature", None),
            "rain_percentage": getattr(packet, "m_rainPercentage", None),
            "weather": str(weather) if weather is not None else None,
            "safety_car_status": self._safe_car_status(packet),
            "forecast_accuracy": getattr(packet, "m_forecastAccuracy", None),
            "weather_forecast": self._weather_forecast(packet),
            "marshal_yellow_zones": self._yellow_zones(packet),
            "pit_window_ideal_lap": getattr(packet, "m_pitStopWindowIdealLap", None),
            "pit_window_latest_lap": getattr(packet, "m_pitStopWindowLatestLap", None),
            "pit_window_rejoin_position": getattr(packet, "m_pitStopRejoinPosition", None),
            "sector2_start_m": getattr(packet, "m_sector2LapDistanceStart", None),
            "sector3_start_m": getattr(packet, "m_sector3LapDistanceStart", None),
            "is_spectating": self.is_spectating,
            "spectator_car_index": self.spectator_car_index,
        }

        if self.total_laps:
            if self.fuel is None:
                self.fuel = FuelRateRecommender([], self.total_laps, self.MIN_FUEL_KG)
            else:
                self.fuel.total_laps = self.total_laps

    @staticmethod
    def _session_kind(session_type) -> str:
        """Map a SessionType enum to practice|qualifying|race|time_trial|unknown.

        Uses the library's own predicates (never str()-compares the enum).
        Sprint shootout rounds are already included in isQualiTypeSession()
        (verified against lib/f1_types/common.py SessionType24).
        """
        try:
            if session_type.isRaceTypeSession():
                return "race"
            if session_type.isQualiTypeSession():
                return "qualifying"
            if session_type.isFpTypeSession():
                return "practice"
            if session_type.isTimeTrialTypeSession():
                return "time_trial"
        except Exception:  # noqa: BLE001
            pass
        return "unknown"

    @staticmethod
    def _safe_car_status(packet) -> str:
        """Readable safety-car status (T2.1). Falls back to the raw value."""
        status = getattr(packet, "m_safetyCarStatus", None)
        if status is None:
            return ""
        try:
            return str(status)
        except Exception:  # noqa: BLE001
            return str(status)

    @staticmethod
    def _weather_forecast(packet) -> List[Dict[str, Any]]:
        """Forecast samples as plain dicts, filtered to the session window (T2.1).

        ``m_timeOffset`` is in minutes. For timed sessions we keep samples up to
        the session duration; for lap-based sessions there is no per-lap offset
        in the struct, so we keep the first hour (approx; documented limit).
        """
        samples = getattr(packet, "m_weatherForecastSamples", None) or []
        duration_s = getattr(packet, "m_sessionDuration", 0) or 0
        horizon_min = (duration_s // 60) if duration_s else 60
        out = []
        for s in samples:
            t_off = getattr(s, "m_timeOffset", None)
            if t_off is None:
                continue
            if horizon_min and t_off > horizon_min:
                continue
            weather = getattr(s, "m_weather", None)
            out.append({
                "time_offset_min": t_off,
                "weather": str(weather) if weather is not None else None,
                "rain_pct": getattr(s, "m_rainPercentage", None),
            })
        return out

    @staticmethod
    def _yellow_zones(packet) -> List[float]:
        """Fraction (0..1) of lap where each yellow marshal zone starts (T2.1).

        MarshalZoneFlagType: 3 = yellow (see packet_1_session_data.py).
        """
        zones = getattr(packet, "m_marshalZones", None) or []
        out = []
        for z in zones:
            flag = getattr(z, "m_zoneFlag", None)
            flag_val = getattr(flag, "value", flag)
            if flag_val == 3:
                start = getattr(z, "m_zoneStart", None)
                if start is not None:
                    out.append(start)
        return out

    def _on_lap_data(self, packet) -> None:
        # Store the full 24-car field first (used by the leaderboard).
        self._all_lap_data = packet.m_lapData
        self._rebuild_leaderboard()

        lap = self._player(packet.m_lapData)
        if lap is None:
            return

        cur_lap = lap.m_currentLapNum
        self.latest["lap"] = {
            "current_lap_num": cur_lap,
            "current_lap_time_ms": lap.m_currentLapTimeInMS,
            "last_lap_time_ms": lap.m_lastLapTimeInMS,
            # s1/s2/s3 via the library's combined-time properties (handles
            # minutes part + "in progress" semantics).
            "sector1_ms": lap.s1TimeMS,
            "sector2_ms": lap.s2TimeMS,
            "sector3_ms": lap.s3TimeMS,
            "car_position": lap.m_carPosition,
            "grid_position": lap.m_gridPosition,
            # Gaps are split into minutes+ms parts in 2026; combine them.
            "delta_to_car_in_front_ms": self._combine(
                lap.m_deltaToCarInFrontInMS, getattr(lap, "m_deltaToCarInFrontMinutes", 0)),
            "delta_to_race_leader_ms": self._combine(
                lap.m_deltaToRaceLeaderInMS, getattr(lap, "m_deltaToRaceLeaderMinutes", 0)),
            "lap_distance_m": lap.m_lapDistance,
            "total_distance_m": lap.m_totalDistance,
            "pit_status": str(lap.m_pitStatus),
            "driver_status": str(lap.m_driverStatus),
            "current_lap_invalid": bool(lap.m_currentLapInvalid),
            "num_pit_stops": lap.m_numPitStops,
            # T2.2: penalties / warnings surfaced for the engineer.
            "penalties_s": getattr(lap, "m_penalties", None),
            "total_warnings": getattr(lap, "m_totalWarnings", None),
            "corner_cutting_warnings": getattr(lap, "m_cornerCuttingWarnings", None),
            "num_unserved_dt_pens": getattr(lap, "m_numUnservedDriveThroughPens", None),
            "num_unserved_sg_pens": getattr(lap, "m_numUnservedStopGoPens", None),
        }

        # T2.5: a FLBK was seen; the next LAP_DATA gives us the (rewound) lap
        # number. Roll back all lap-derived data at/after it before continuing.
        if self._pending_flashback:
            self._pending_flashback = False
            self._rollback_to_lap(cur_lap)

        # Feed the delta manager with distance/time samples on the current lap.
        self.delta.record_data_point(
            cur_lap, lap.m_lapDistance, lap.m_currentLapTimeInMS
        )

        # Detect lap boundary: feed completed lap time + fuel into trend history.
        # The completed lap's validity is the invalid flag as last seen on that
        # lap (the packet that increments the lap number already describes the
        # NEW lap, so we cannot read it there).
        if self._last_current_lap_num is not None and cur_lap == self._last_current_lap_num + 1:
            self._on_lap_completed(self._last_current_lap_num, lap.m_lastLapTimeInMS,
                                   valid=not self._last_lap_invalid)
        self._last_current_lap_num = cur_lap
        self._last_lap_invalid = bool(lap.m_currentLapInvalid)

    def _on_lap_completed(self, completed_lap_num: int, last_lap_ms: int,
                          valid: bool = True) -> None:
        if last_lap_ms and last_lap_ms > 0:
            # Every completed lap is recorded (invalid ones marked as such), but
            # only valid laps may feed the pace trend and the best-lap reference
            # - a cut/invalid lap must never become the delta baseline.
            self.lap_records.push({
                "lap_num": completed_lap_num,
                "lap_time_ms": last_lap_ms,
                "valid": valid,
            })
            if valid:
                self.lap_times_ms.push(last_lap_ms)
                if self._best_lap_ms is None or last_lap_ms < self._best_lap_ms:
                    self._best_lap_ms = last_lap_ms
                    # best lap reference is the lap that just ended
                    self.delta.set_best_lap(completed_lap_num)

        # T1.4: record per-lap fuel burn and max tyre wear. Previously both
        # RollingHistories were declared but never pushed, so the wear/fuel
        # trends simply did not exist.
        #
        # Fuel: end-of-lap fuel is sampled in _on_car_status into
        # _fuel_at_lap_end keyed by lap number. This lap's burn is the drop
        # from the previous lap's end value.
        fuel_this = self._fuel_at_lap_end.get(completed_lap_num)
        fuel_prev = self._fuel_at_lap_end.get(completed_lap_num - 1)
        if fuel_this is not None and fuel_prev is not None:
            burned = fuel_prev - fuel_this
            if burned >= 0:
                self.fuel_per_lap.push(round(burned, 3))

        # Tyre wear: the max of the four wheels' wear as last seen. The damage
        # packet is the only source; skip when no damage packet has arrived yet.
        dmg = self.latest.get("damage") or {}
        wear_vals = [dmg.get(k) for k in
                     ("tyre_wear_fl", "tyre_wear_fr", "tyre_wear_rl", "tyre_wear_rr")]
        wear_vals = [v for v in wear_vals if isinstance(v, (int, float))]
        wear_max = max(wear_vals) if wear_vals else None
        if wear_max is not None:
            self.tyre_wear_per_lap.push(wear_max)

        # T4.1: full per-lap snapshot for the race model / debrief (kept as a
        # plain list so flashback rewinding is trivial).
        self._record_lap_snapshot(completed_lap_num, last_lap_ms, valid,
                                  fuel_this=fuel_this, wear_max=wear_max)

    def _record_lap_snapshot(self, lap_num: int, lap_ms: int, valid: bool,
                             fuel_this: Optional[float] = None,
                             wear_max: Optional[float] = None) -> None:
        """Append a complete per-lap sample (T4.1). Bounded to 100 entries."""
        status = self.latest.get("status") or {}
        lap = self.latest.get("lap") or {}
        pos_ctx = self.latest.get("position_context") or {}
        ahead = pos_ctx.get("ahead") or {}
        behind = pos_ctx.get("behind") or {}
        fuel_prev = self._fuel_at_lap_end.get(lap_num - 1)
        burn = (fuel_prev - fuel_this) if (fuel_prev is not None and fuel_this is not None) else None
        self.lap_snapshots.append({
            "lap_num": lap_num,
            "lap_time_ms": lap_ms,
            "valid": valid,
            "tyre_compound": status.get("tyre_compound_actual"),
            "tyre_age_laps": status.get("tyres_age_laps"),
            "tyre_wear_max_pct": wear_max,
            "fuel_kg": fuel_this,
            "fuel_burn_kg": round(burn, 3) if burn is not None and burn >= 0 else None,
            "position": lap.get("car_position"),
            "gap_ahead_ms": ahead.get("gap_ms"),
            "ahead_index": (ahead.get("car_index") if ahead else None),
            "gap_behind_ms": behind.get("gap_ms"),
            "behind_index": (behind.get("car_index") if behind else None),
            "safety_car": (self.latest.get("session") or {}).get("safety_car_status"),
            "pit_this_lap": (lap.get("pit_status") not in (None, "NONE")),
        })
        self.lap_snapshots = self.lap_snapshots[-100:]

    def _on_car_telemetry(self, packet) -> None:
        car = self._player(packet.m_carTelemetryData)
        if car is None:
            return
        self.latest["car"] = {
            "speed_kph": car.m_speed,
            "gear": car.m_gear,
            "rpm": car.m_engineRPM,
            "throttle": car.m_throttle,
            "brake": car.m_brake,
            "steer": car.m_steer,
            "drs": car.m_drs,
            "tyres_surface_temp_c": list(car.m_tyresSurfaceTemperature),
            "tyres_inner_temp_c": list(car.m_tyresInnerTemperature),
            "tyres_pressure_psi": list(car.m_tyresPressure),
            "engine_temp_c": car.m_engineTemperature,
        }
        # T1.3: sample inner temps for the ~3s median used by the tyre notes.
        inner = list(car.m_tyresInnerTemperature)
        if inner:
            self._tyre_inner_samples.append((self._clock(), inner))
            cutoff = self._clock() - self.TYRE_TEMP_MEDIAN_WINDOW_S
            while self._tyre_inner_samples and self._tyre_inner_samples[0][0] < cutoff:
                self._tyre_inner_samples.popleft()
        # T5.7: track "straight since" for the radio timing gate. Updated at
        # the telemetry rate (much finer than the 2Hz snapshot).
        self._update_straight(car.m_throttle, car.m_brake, car.m_steer)

    # Config lookups take the config lock; the gate thresholds are re-read at
    # most this often (the receive thread calls _straight_thresholds at ~60Hz).
    _STRAIGHT_CFG_TTL_S = 5.0

    def _straight_thresholds(self):
        cached = self._straight_cfg
        if cached is not None and (self._clock() - cached[0]) < self._STRAIGHT_CFG_TTL_S:
            return cached[1]
        try:
            from config import get_config
            cfg = get_config()
            vals = (cfg.get_float("RADIO_GATE_THROTTLE", 0.9),
                    cfg.get_float("RADIO_GATE_BRAKE", 0.05),
                    cfg.get_float("RADIO_GATE_STEER", 0.15))
        except Exception:  # noqa: BLE001
            vals = (0.9, 0.05, 0.15)
        self._straight_cfg = (self._clock(), vals)
        return vals

    def _update_straight(self, throttle, brake, steer) -> None:
        thr, brk, st = self._straight_thresholds()
        now = self._clock()
        on = (throttle is not None and throttle >= thr
              and brake is not None and brake <= brk
              and steer is not None and abs(steer) <= st)
        if on:
            if self._straight_since is None:
                self._straight_since = now
        else:
            self._straight_since = None

    def is_on_straight(self, hold_s: float = 0.5,
                       clock: Optional[Callable[[], float]] = None) -> bool:
        """True when the car has been on a straight for at least ``hold_s`` (T5.7).

        Polled by the arbiter at ~20Hz; reads the fine-grained timestamp the
        telemetry handler maintains (not the 2Hz snapshot).
        """
        if self._straight_since is None:
            return False
        now = (clock or self._clock)()
        return (now - self._straight_since) >= hold_s

    def tyre_inner_temp_median_c(self) -> Optional[List[float]]:
        """Median inner temperature per wheel over the last ~3s (T1.3).

        Returns ``[FL, FR, RL, RR]`` (rounded) or None when no samples exist.
        A rolling median suppresses short brake-zone spikes that made the
        instantaneous surface-temperature check misfire.
        """
        if not self._tyre_inner_samples:
            return None
        cutoff = self._clock() - self.TYRE_TEMP_MEDIAN_WINDOW_S
        recent = [s for ts, s in self._tyre_inner_samples if ts >= cutoff]
        if not recent:
            recent = [self._tyre_inner_samples[-1][1]]
        wheels = len(recent[0])
        out = []
        for w in range(wheels):
            col = [s[w] for s in recent if w < len(s)]
            out.append(round(statistics.median(col), 1) if col else None)
        return out

    def _on_car_status(self, packet) -> None:
        st = self._player(packet.m_carStatusData)
        if st is None:
            return
        self._all_car_status = packet.m_carStatusData
        self._rebuild_leaderboard()
        self.latest["status"] = {
            "fuel_in_tank_kg": st.m_fuelInTank,
            "fuel_capacity_kg": st.m_fuelCapacity,
            "fuel_remaining_laps": st.m_fuelRemainingLaps,
            "tyre_compound_actual": str(st.m_actualTyreCompound),
            "tyre_compound_visual": str(st.m_visualTyreCompound),
            "tyres_age_laps": st.m_tyresAgeLaps,
            "ers_store_energy_j": st.m_ersStoreEnergy,
            "ers_deploy_mode": str(st.m_ersDeployMode),
            "ers_deployed_this_lap_j": getattr(st, "m_ersDeployedThisLap", None),
            "drs_allowed": st.m_drsAllowed,
            "pit_limiter": st.m_pitLimiterStatus,
        }
        # Fuel is sampled here (car-status arrives ~every frame) and bucketed by
        # the lap number we last saw on the lap-data packet. When the lap number
        # advances, the previous bucket holds that lap's *ending* fuel, which is
        # exactly what FuelRateRecommender wants.
        self._fuel_lap_num = self._last_current_lap_num
        if self._fuel_lap_num is not None:
            self._fuel_at_lap_end[self._fuel_lap_num] = st.m_fuelInTank
            self._ingest_fuel_history()

    def _ingest_fuel_history(self) -> None:
        """Feed completed per-lap fuel readings into the fuel recommender.

        Only laps strictly older than the current lap are complete. We add any
        completed lap we haven't recorded yet (normal + flashback rewind).
        """
        # Lazily create the recommender; total_laps may arrive later and is
        # updated via the setter, so a placeholder value is fine.
        if self.fuel is None:
            if not self._fuel_at_lap_end:
                return
            self.fuel = FuelRateRecommender([], self.total_laps or 1, self.MIN_FUEL_KG)
        elif self.total_laps:
            self.fuel.total_laps = self.total_laps

        current = self._last_current_lap_num
        for lap_num in sorted(self._fuel_at_lap_end):
            if current is not None and lap_num >= current:
                continue  # lap still in progress
            if lap_num in self._fuel_recorded:
                continue
            self.fuel.add(self._fuel_at_lap_end[lap_num], lap_num, is_racing_lap=True)
            self._fuel_recorded.add(lap_num)
        # Drop readings for laps older than what we keep.
        stale = [n for n in self._fuel_at_lap_end if current is not None and n < current - 2]
        for n in stale:
            del self._fuel_at_lap_end[n]

    def _on_car_damage(self, packet) -> None:
        dmg = self._player(packet.m_carDamageData)
        if dmg is None:
            return
        tw = dmg.m_tyresWear
        td = getattr(dmg, "m_tyresDamage", None)
        bl = getattr(dmg, "m_tyreBlisters", None)
        self.latest["damage"] = {
            # Bodywork / aero (0-100 %)
            "front_left_wing": dmg.m_frontLeftWingDamage,
            "front_right_wing": dmg.m_frontRightWingDamage,
            "rear_wing": dmg.m_rearWingDamage,
            "floor": dmg.m_floorDamage,
            "diffuser": getattr(dmg, "m_diffuserDamage", None),
            "sidepod": dmg.m_sidepodDamage,
            "drs_fault": bool(getattr(dmg, "m_drsFault", False)),
            # Power unit / gearbox (0-100 % wear)
            "engine": dmg.m_engineDamage,
            "gearbox": dmg.m_gearBoxDamage,
            # Flags
            "engine_blown": bool(getattr(dmg, "m_engineBlown", False)),
            "engine_seized": bool(getattr(dmg, "m_engineSeized", False)),
            "ers_fault": bool(getattr(dmg, "m_ersFault", False)),
            # Tyres
            "tyre_wear_fl": tw[0] if tw else None,
            "tyre_wear_fr": tw[1] if tw else None,
            "tyre_wear_rl": tw[2] if tw else None,
            "tyre_wear_rr": tw[3] if tw else None,
            "tyre_damage_avg": (sum(td) / len(td)) if td else None,
            "tyre_blister_max": max(bl) if bl else None,
        }
        # Derived: worst bodywork damage + a rough "significant damage" flag.
        body = [self.latest["damage"][k] for k in
                ("front_left_wing", "front_right_wing", "rear_wing", "floor", "sidepod")
                if self.latest["damage"].get(k) is not None]
        self.latest["damage"]["worst_bodywork"] = max(body) if body else None
        self.latest["damage"]["has_significant_damage"] = any(v >= 20 for v in body) if body else False

    def _on_car_telemetry_2(self, packet) -> None:
        """F1 2026 active aero + overtake telemetry (packet ID 16)."""
        car = self._player(packet.m_carTelemetry2Data)
        if car is None:
            return
        self.latest["car2"] = {
            "active_aero_mode": str(car.m_activeAeroMode),
            "active_aero_available": bool(car.m_activeAeroAvailable),
            "active_aero_activation_distance_m": car.m_activeAeroActivationDistance,
            "overtake_available": bool(car.m_overtakeAvailable),
            "overtake_active": bool(car.m_overtakeActive),
            "overtake_activation_distance_m": car.m_overtakeActivationDistance,
            "regulations_2026": bool(car.m_2026Regulations),
            "driving_wrong_way": bool(car.m_drivingWrongWay),
        }

    def _on_session_history(self, packet) -> None:
        """Per-lap history (times, sectors, tyre stints) for the player's car."""
        if packet.m_carIdx != self.player_car_index:
            return
        laps = []
        for i, h in enumerate(packet.m_lapHistoryData):
            if h.m_lapTimeInMS <= 0:
                continue
            laps.append({
                "lap_num": i + 1,
                "lap_time_ms": h.m_lapTimeInMS,
                "sector1_ms": self._combine(h.m_sector1TimeInMS, h.m_sector1TimeMinutes),
                "sector2_ms": self._combine(h.m_sector2TimeInMS, h.m_sector2TimeMinutes),
                "sector3_ms": self._combine(h.m_sector3TimeInMS, h.m_sector3TimeMinutes),
                # Bit 0x01 = "lap valid"; the other bits are per-sector validity
                # and must NOT make an invalid lap look valid.
                "valid": bool(h.m_lapValidBitFlags & LapHistoryData.FULL_LAP_VALID_BIT_MASK),
            })
        stints = []
        for s in packet.m_tyreStintsHistoryData:
            end_lap = s.m_endLap
            stints.append({
                # 255 is the spec's sentinel for "this is the current tyre".
                "end_lap": "current" if end_lap == 255 else end_lap,
                "compound_actual": str(getattr(s, "m_tyreActualCompound", "")),
                "compound_visual": str(getattr(s, "m_tyreVisualCompound", "")),
            })
        # Only trust the game's best-lap pointer if it actually points at a
        # valid lap (it should, but a stale/edge value must not leak through).
        best_lap_num = packet.m_bestLapTimeLapNum
        if best_lap_num and best_lap_num not in {l["lap_num"] for l in laps if l["valid"]}:
            best_lap_num = None
        self.latest["history"] = {
            "num_laps": packet.m_numLaps,
            "num_tyre_stints": packet.m_numTyreStints,
            "best_lap_time_lap_num": best_lap_num,
            "laps": laps,
            "stints": stints,
        }

    def _on_time_trial(self, packet) -> None:
        """Time-trial personal best / session best / rival comparison."""
        self.latest["time_trial"] = {
            "player_session_best_ms": getattr(
                packet.m_playerSessionBestDataSet, "m_lapTimeInMS", None),
            "personal_best_ms": getattr(
                packet.m_personalBestDataSet, "m_lapTimeInMS", None),
            "rival_session_best_ms": getattr(
                packet.m_rivalSessionBestDataSet, "m_lapTimeInMS", None),
        }

    def _on_final_classification(self, packet) -> None:
        """T2.6: store the final classification (also a debrief trigger)."""
        rows = []
        for idx, c in enumerate(packet.m_classificationData):
            rows.append({
                "position": c.m_position,
                "car_index": idx,
                "driver": self._driver_name(idx),
                "num_laps": c.m_numLaps,
                "grid_position": c.m_gridPosition,
                "points": c.m_points,
                "num_pit_stops": c.m_numPitStops,
                "result_status": str(c.m_resultStatus),
                "best_lap_ms": c.m_bestLapTimeInMS,
                "total_race_time_s": c.m_totalRaceTime,
                "penalties_time_s": c.m_penaltiesTime,
                "num_penalties": c.m_numPenalties,
                "num_tyre_stints": c.m_numTyreStints,
                "is_player": idx == self.player_car_index,
            })
        rows.sort(key=lambda r: r["position"])
        self.latest["final_classification"] = rows

    @staticmethod
    def _combine(ms_part: int, min_part: int) -> int:
        if not ms_part and not min_part:
            return 0
        return (min_part or 0) * 60000 + (ms_part or 0)

    # ----------------------------------------------------------------- events

    def _on_event(self, packet) -> None:
        """Handle EVENT packets (T2.4: dispatch by event code).

        OVERTAKE (OVTK) keeps its exact previous behaviour and text (the
        authoritative "who overtook whom" source, preferred over the
        leaderboard diff fallback). Every event now also carries
        ``session_time`` (header) and ``lap_num`` for the timeline.
        """
        code = getattr(packet, "m_eventCode", None)
        details = getattr(packet, "mEventDetails", None)
        EPT = PacketEventData.EventPacketType

        if code == EPT.OVERTAKE:
            self._on_event_overtake(details)
        elif code == EPT.SAFETY_CAR:
            self._on_event_safety_car(details)
        elif code == EPT.RED_FLAG:
            self._add_event("red_flag", None, None, "红旗出示")
        elif code == EPT.PENALTY_ISSUED:
            self._on_event_penalty(details)
        elif code == EPT.FASTEST_LAP:
            self._on_event_fastest_lap(details)
        elif code == EPT.RETIREMENT:
            self._on_event_retirement(details)
        elif code == EPT.CHEQUERED_FLAG:
            self._add_event("chequered", None, None, "方格旗，比赛结束")
        elif code == EPT.LIGHTS_OUT:
            self._add_event("lights_out", None, None, "起步！")
        elif code == EPT.SESSION_STARTED:
            self._add_event("session_started", None, None, "会话开始")
        elif code == EPT.SESSION_ENDED:
            self._add_event("session_ended", None, None, "会话结束")
        elif code == EPT.DRIVE_THROUGH_SERVED:
            self._on_event_dt_served(details)
        elif code == EPT.STOP_GO_SERVED:
            self._on_event_sg_served(details)
        elif code == EPT.FLASHBACK:
            self._on_event_flashback(details)
        # else: unhandled event code - ignored (must never raise).

    def _event_meta(self, packet) -> Dict[str, Any]:
        header = getattr(packet, "m_header", None)
        st = getattr(header, "m_sessionTime", None) if header is not None else None
        lap = (self.latest.get("lap") or {}).get("current_lap_num")
        return {"session_time": st, "lap_num": lap}

    def _on_event_overtake(self, details) -> None:
        overtaker = getattr(details, "overtakingVehicleIdx", None)
        overtaken = getattr(details, "beingOvertakenVehicleIdx", None)
        if overtaker is None or overtaken is None:
            return

        focus = self.player_car_index
        overtaker_name = self._driver_name(overtaker)
        overtaken_name = self._driver_name(overtaken)
        if overtaker == focus:
            kind, text = "position_up", f"你超过了 {overtaken_name}"
            self._arm_diff_dedup("up")
        elif overtaken == focus:
            kind, text = "position_down", f"你被 {overtaker_name} 超过"
            self._arm_diff_dedup("down")
        else:
            kind, text = "field_overtake", f"{overtaker_name} 超过了 {overtaken_name}"

        # from/to carry the two vehicle indices for official overtake events
        # (for diff-derived events they carry positions).
        self._add_event(kind, overtaker, overtaken, text)

    def _on_event_safety_car(self, details) -> None:
        sc_type = getattr(details, "m_safety_car_type", None)
        ev_type = getattr(details, "m_event_type", None)
        sc_val = getattr(sc_type, "value", sc_type)
        ev_val = getattr(ev_type, "value", ev_type)
        name = "安全车" if sc_val == 1 else ("虚拟安全车" if sc_val == 2 else "安全车")
        if ev_val == 0:      # DEPLOYED
            text = f"{name}出动"
        elif ev_val == 1:    # RETURNING
            text = f"{name}准备返回"
        elif ev_val == 2:    # RETURNED
            text = f"{name}结束"
        elif ev_val == 3:    # RESUME_RACE
            text = "比赛恢复"
        else:
            text = f"{name}状态更新"
        self._add_event("safety_car", sc_val, ev_val, text,
                        extra={"safety_car_type": sc_val, "event_type": ev_val})

    def _on_event_penalty(self, details) -> None:
        veh = getattr(details, "vehicleIdx", None)
        ptype = getattr(details, "penaltyType", None)
        ptype_val = getattr(ptype, "value", ptype)
        ptime = getattr(details, "time", None)
        who = self._driver_name(veh) if veh is not None else "?"
        is_player = veh == self.player_car_index
        text = f"{'你' if is_player else who}被罚时" + (f" {ptime}s" if ptime else "")
        self._add_event("penalty", veh, ptype_val, text,
                        extra={"vehicle_idx": veh, "penalty_type": ptype_val,
                               "infringement_type": getattr(details, "infringementType", None),
                               "penalty_time_s": ptime, "is_player": is_player})

    def _on_event_fastest_lap(self, details) -> None:
        veh = getattr(details, "vehicleIdx", None)
        lap_time_s = getattr(details, "lapTime", None)
        who = self._driver_name(veh) if veh is not None else "?"
        is_player = veh == self.player_car_index
        ms = int(lap_time_s * 1000) if isinstance(lap_time_s, (int, float)) else None
        text = f"{'你' if is_player else who}刷出最快圈"
        self._add_event("fastest_lap", veh, ms, text,
                        extra={"vehicle_idx": veh, "lap_time_ms": ms,
                               "is_player": is_player})

    def _on_event_retirement(self, details) -> None:
        veh = getattr(details, "vehicleIdx", None)
        who = self._driver_name(veh) if veh is not None else "?"
        is_player = veh == self.player_car_index
        text = f"{'你' if is_player else who}退赛"
        self._add_event("retirement", veh, None, text,
                        extra={"vehicle_idx": veh, "is_player": is_player,
                               "driver": who})

    def _on_event_dt_served(self, details) -> None:
        veh = getattr(details, "vehicleIdx", None)
        self._add_event("penalty_served", veh, "DT", "通过处罚已执行",
                        extra={"vehicle_idx": veh, "kind": "drive_through"})

    def _on_event_sg_served(self, details) -> None:
        veh = getattr(details, "vehicleIdx", None)
        self._add_event("penalty_served", veh, "SG", "停走处罚已执行",
                        extra={"vehicle_idx": veh, "kind": "stop_go"})

    def _on_event_flashback(self, details) -> None:
        """T2.5: mark a pending rollback; the lap number at this instant is not
        trustworthy, so the actual truncation happens on the next LAP_DATA."""
        self._pending_flashback = True
        # The first LAP_DATA after the rewind carries post-rewind positions;
        # rebuilding the baseline here (instead of diffing against the
        # pre-rewind position) avoids a fake "dropped N places" event.
        self._last_position = None
        self._add_event("flashback", None, None, "回放（数据回滚）",
                        extra={"flashback_time": getattr(details, "flashbackSessionTime", None)})

    def _add_event(self, kind: str, frm: Any, to: Any, text: str,
                   extra: Optional[Dict[str, Any]] = None) -> None:
        """Append an event with the timeline fields, trimming to the last 20."""
        self._event_seq += 1
        ev: Dict[str, Any] = {
            "seq": self._event_seq,
            "kind": kind,
            "from": frm,
            "to": to,
            "text": text,
            "session_time": self._event_session_time,
            "lap_num": (self.latest.get("lap") or {}).get("current_lap_num"),
        }
        if extra:
            ev.update(extra)
        self.events.append(ev)
        self.events = self.events[-20:]

    def _arm_diff_dedup(self, direction: str) -> None:
        """Remember that an official player-involved overtake was just recorded,
        so the next matching leaderboard diff is not double-reported."""
        self._pending_diff_event = direction
        self._pending_diff_until = self._clock() + self.OFFICIAL_EVENT_DEDUP_S

    def _driver_name(self, idx: int) -> str:
        pinfo = self._participants.get(idx)
        name = (pinfo or {}).get("name")
        return name or f"car{idx}"

    def _rollback_to_lap(self, current_lap: int) -> None:
        """Rewind lap-derived data after a flashback (T2.5).

        Everything derived from laps at or after ``current_lap`` is discarded:
        lap_records, best-lap baseline, pace/fuel/wear trends, the fuel
        recommender and the per-lap snapshots. ``current_lap`` is the lap the
        car is on *after* the rewind.
        """
        # 1) lap_records (RollingHistory has no key delete -> rebuild).
        kept = [r for r in self.lap_records.values()
                if r.get("lap_num", 0) < current_lap]
        self.lap_records.clear()
        for r in kept:
            self.lap_records.push(r)

        # 2) best-lap baseline + pace trend from the surviving VALID laps.
        valid_ms = [r["lap_time_ms"] for r in kept if r.get("valid")]
        self.lap_times_ms.clear()
        for ms in valid_ms:
            self.lap_times_ms.push(ms)
        if valid_ms:
            self._best_lap_ms = min(valid_ms)
            best_lap_num = next(r["lap_num"] for r in kept
                                if r.get("valid") and r["lap_time_ms"] == self._best_lap_ms)
            self.delta = LapDeltaManager()
            self.delta.set_best_lap(best_lap_num)
        else:
            self._best_lap_ms = None
            self.delta = LapDeltaManager()

        # 3) fuel bookkeeping: drop readings for rewound laps and rebuild.
        self._fuel_at_lap_end = {n: f for n, f in self._fuel_at_lap_end.items()
                                 if n < current_lap}
        self._fuel_recorded = {n for n in self._fuel_recorded if n < current_lap}
        self._rebuild_fuel_recommender()

        # 4) per-lap trend histories: recompute from the surviving snapshots.
        self.lap_snapshots = [s for s in self.lap_snapshots
                              if s.get("lap_num", 0) < current_lap]
        self.fuel_per_lap.clear()
        self.tyre_wear_per_lap.clear()
        for s in self.lap_snapshots:
            if s.get("fuel_burn_kg") is not None:
                self.fuel_per_lap.push(s["fuel_burn_kg"])
            if s.get("tyre_wear_max_pct") is not None:
                self.tyre_wear_per_lap.push(s["tyre_wear_max_pct"])

    def _rebuild_fuel_recommender(self) -> None:
        """Rebuild FuelRateRecommender from the surviving end-of-lap readings.

        The recommender cannot rewind itself, so after a flashback we throw it
        away and replay the remaining laps into a fresh instance.
        """
        if not self._fuel_at_lap_end:
            self.fuel = None
            return
        self.fuel = FuelRateRecommender([], self.total_laps or 1, self.MIN_FUEL_KG)
        current = self._last_current_lap_num
        for lap_num in sorted(self._fuel_at_lap_end):
            if current is not None and lap_num >= current:
                continue
            self.fuel.add(self._fuel_at_lap_end[lap_num], lap_num, is_racing_lap=True)
            self._fuel_recorded.add(lap_num)

    # ----------------------------------------------------------- leaderboard

    def _on_participants(self, packet) -> None:
        """Store car index -> driver info (name, team, ai/human)."""
        info: Dict[int, Dict[str, Any]] = {}
        for idx, p in enumerate(packet.m_participants):
            try:
                name = p.name
            except Exception:
                name = getattr(p, "m_name", None)
            info[idx] = {
                "name": name,
                "team": str(getattr(p, "m_teamId", "")),
                "race_number": getattr(p, "m_raceNumber", None),
                "is_ai": bool(getattr(p, "m_aiControlled", False)),
            }
        self._participants = info
        self._rebuild_leaderboard()

    def _rebuild_leaderboard(self) -> None:
        """Join LAP_DATA (position/gap) + PARTICIPANTS (name) + CAR_STATUS (tyres)."""
        lap_data = self._all_lap_data
        if not lap_data:
            return
        status_data = self._all_car_status

        rows = []
        for idx, lap in enumerate(lap_data):
            pos = lap.m_carPosition
            if pos is None or pos <= 0:
                continue
            result = str(getattr(lap, "m_resultStatus", ""))
            if result in ("INACTIVE", "RETIRED", "DID_NOT_FINISH", "DISQUALIFIED"):
                continue

            pinfo = self._participants.get(idx, {})
            last_lap = lap.m_lastLapTimeInMS
            # T2.3: track the best (min) non-zero last-lap time per car.
            if last_lap and last_lap > 0:
                prev = self._best_lap_by_car.get(idx)
                if prev is None or last_lap < prev:
                    self._best_lap_by_car[idx] = last_lap
            drv_status = getattr(lap, "m_driverStatus", None)
            row: Dict[str, Any] = {
                "position": pos,
                "car_index": idx,
                "driver": pinfo.get("name") or f"car{idx}",
                "team": pinfo.get("team"),
                "is_player": idx == self.player_car_index,
                "lap_num": lap.m_currentLapNum,
                "gap_to_leader_ms": self._combine(
                    lap.m_deltaToRaceLeaderInMS,
                    getattr(lap, "m_deltaToRaceLeaderMinutes", 0)),
                "gap_to_front_ms": self._combine(
                    lap.m_deltaToCarInFrontInMS,
                    getattr(lap, "m_deltaToCarInFrontMinutes", 0)),
                "last_lap_ms": last_lap,
                "best_lap_ms": self._best_lap_by_car.get(idx),
                "pit_status": str(lap.m_pitStatus),
                "driver_status": str(drv_status) if drv_status is not None else None,
                "lap_distance_m": getattr(lap, "m_lapDistance", None),
                "penalties_s": getattr(lap, "m_penalties", None),
                "tyre": None,
                "tyre_age": None,
            }
            if status_data and idx < len(status_data):
                st = status_data[idx]
                row["tyre"] = str(getattr(st, "m_actualTyreCompound", ""))
                row["tyre_age"] = getattr(st, "m_tyresAgeLaps", None)
            rows.append(row)

        rows.sort(key=lambda r: r["position"])
        self.leaderboard = rows

        # Detect the player's position change -> an event the AI can report.
        # While a flashback rollback is pending, the lap data behind this
        # rebuild is still PRE-rewind: diffing it (or latching its position as
        # the new baseline) would fire a fake "dropped N places" event once the
        # rewound LAP_DATA arrives. Stay silent until the rollback has run.
        player_row = next((r for r in rows if r["is_player"]), None)
        if player_row and not self._pending_flashback:
            new_pos = player_row["position"]
            if self._last_position is not None and new_pos != self._last_position:
                self._maybe_record_position_event(self._last_position, new_pos, rows)
            self._last_position = new_pos

        # Player's relative gaps to neighbours.
        player = player_row
        if player:
            # Car ahead: our gap-to-front = how far *we* are behind them.
            ahead = self._neighbour(rows, player["position"], -1)
            if ahead:
                ahead["gap_ms"] = player["gap_to_front_ms"]
            # Car behind: their gap-to-front = how far *they* are behind us.
            behind = self._neighbour(rows, player["position"], +1)
            self.latest["position_context"] = {
                "position": player["position"],
                "gap_to_leader_ms": player["gap_to_leader_ms"],
                "gap_to_front_ms": player["gap_to_front_ms"],
                "ahead": ahead,
                "behind": behind,
            }

    def _neighbour(self, rows: list, pos: int, direction: int) -> Optional[Dict[str, Any]]:
        """Return the car directly ahead (direction=-1) or behind (+1)."""
        target = pos + direction
        for r in rows:
            if r["position"] == target:
                return {"driver": r["driver"], "position": r["position"],
                        "car_index": r.get("car_index"),
                        "gap_ms": r["gap_to_front_ms"]}
        return None

    def _maybe_record_position_event(self, old_pos: int, new_pos: int, rows: list) -> None:
        """Record a position-change event from the leaderboard diff, unless the
        same change was already reported via an official OVERTAKE event."""
        pending = self._pending_diff_event
        if pending is not None:
            expiry = self._pending_diff_until
            self._pending_diff_event = None
            self._pending_diff_until = 0.0
            direction = "up" if new_pos < old_pos else "down"
            if direction == pending and self._clock() <= expiry:
                return  # duplicate of the official OVERTAKE event - skip
        self._record_position_event(old_pos, new_pos, rows)

    def _record_position_event(self, old_pos: int, new_pos: int, rows: list) -> None:
        """Record a gained/lost-place event with the other driver's name."""
        if new_pos < old_pos:
            # We gained (new_pos - old_pos) places; the car we passed sits at old_pos.
            other = next((r for r in rows if r["position"] == old_pos), None)
            text = (f"你上升了 {old_pos - new_pos} 位到 P{new_pos}"
                    + (f"，超过了 {other['driver']}" if other else ""))
            kind = "position_up"
        else:
            other = next((r for r in rows if r["position"] == old_pos), None)
            text = (f"你下降了 {new_pos - old_pos} 位到 P{new_pos}"
                    + (f"，被 {other['driver']} 超过" if other else ""))
            kind = "position_down"
        self._add_event(kind, old_pos, new_pos, text)

    # -------------------------------------------------------------- snapshot

    # How often (max) the frozen snapshot is rebuilt.
    SNAPSHOT_HZ = 2.0

    def mark_dirty(self) -> None:
        """Flag that live state changed and the frozen copy is stale."""
        self._snap_dirty = True

    def refresh_snapshot(self, force: bool = False) -> None:
        """Rebuild the frozen snapshot if stale and older than 1/SNAPSHOT_HZ.

        Called from the receiver thread after each accepted packet; cheap when
        throttled (a couple of deep copies per second, not per packet).
        """
        now = self._clock()
        if not force and not self._snap_dirty:
            return
        if not force and (now - self._frozen_at) < (1.0 / self.SNAPSHOT_HZ):
            return
        with self._state_lock:   # no writer can run while we copy live state
            frozen = self._build_snapshot()
            with self._snap_lock:
                self._frozen = frozen
                self._frozen_at = now
                self._snap_dirty = False

    def snapshot(self) -> Dict[str, Any]:
        """Return a frozen state snapshot (deep copy, safe to read anywhere).

        Correctness first: if the live state changed since the frozen copy was
        built (``_snap_dirty``), the caller gets a FRESH copy (read-your-
        writes). Rebuilds are single-flighted through ``_build_lock`` so N
        concurrent readers cause at most ONE rebuild; once a rebuild finishes
        the waiting readers reuse it. When the state is clean (the receiver
        just published and nothing changed since), the frozen copy is reused
        with no copy at all — that is the cheap path that keeps frequent
        polling from stalling the receive loop.
        """
        with self._snap_lock:
            if self._frozen and not self._snap_dirty:
                return self._frozen           # clean: reuse, zero cost
        with self._build_lock:                # one rebuild at a time
            with self._snap_lock:
                if self._frozen and not self._snap_dirty:
                    return self._frozen       # someone rebuilt while we waited
            self.refresh_snapshot(force=True)
            with self._snap_lock:
                return self._frozen

    def _build_snapshot(self) -> Dict[str, Any]:
        """Build a deep-copied snapshot from the live state (writer thread)."""
        delta = self.delta.get_delta()
        fuel: Dict[str, Any] = {}
        if self.fuel is not None:
            fuel = {
                "curr_fuel_rate_kg_per_lap": self.fuel.curr_fuel_rate,
                "target_fuel_rate_kg_per_lap": self.fuel.target_fuel_rate,
                "fuel_used_last_lap_kg": self.fuel.fuel_used_last_lap,
                "surplus_laps": self.fuel.surplus_laps,
                "predicted_final_fuel_kg": self.fuel.final_fuel_kg,
                "data_sufficient": self.fuel.isDataSufficient(),
            }

        latest = copy.deepcopy(self.latest)
        # T1.3: attach the smoothed inner-tyre median next to the raw car data.
        car = latest.get("car")
        if isinstance(car, dict):
            car["tyres_inner_temp_median_c"] = self.tyre_inner_temp_median_c()

        # T3.8: merge extension providers (race model etc.) into the snapshot.
        extra: Dict[str, Any] = {}
        for fn in list(self._snapshot_providers):
            try:
                result = fn()
                if isinstance(result, dict):
                    # Providers return {namespace: value}; merge each key.
                    extra.update(copy.deepcopy(result))
            except Exception as e:  # noqa: BLE001 - a bad provider must not break RX
                # Rate-limited trace: a permanently failing provider (e.g. the
                # race model) used to vanish without a word.
                self._provider_errors = getattr(self, "_provider_errors", 0) + 1
                if (self._error_logger is not None
                        and (self._provider_errors <= 3
                             or self._provider_errors % 100 == 0)):
                    self._error_logger.warning(
                        "snapshot provider failed (%d): %r",
                        self._provider_errors, e)

        return {
            "session": {
                "session_uid": self.session_uid,
                "session_type": self.session_type,
                "track_id": str(self.track_id) if self.track_id is not None else None,
                "total_laps": self.total_laps,
                "player_car_index": self.player_car_index,
            },
            # Deep copies: consumers must never hold a live reference into the
            # mutating state (the old snapshot() exposed self.latest directly).
            "latest": latest,
            "delta": None if delta is None else {
                "delta_ms": delta.delta_ms,
                "best_lap_num": delta.best_lap_num,
                "distance_m": delta.distance_m,
            },
            "fuel": dict(fuel),
            "trends": {
                "lap_times_ms": list(self.lap_times_ms.values()),
                "best_lap_ms": self._best_lap_ms,
                # All completed laps incl. invalid ones (each {lap_num,
                # lap_time_ms, valid}); lap_times_ms above is valid-only.
                "lap_records": list(self.lap_records.values()),
                # T1.4: per-lap fuel burn (kg) and max tyre wear (%), newest last.
                "fuel_per_lap_kg": list(self.fuel_per_lap.values()),
                "tyre_wear_per_lap_pct": list(self.tyre_wear_per_lap.values()),
            },
            "packet_counts": dict(self.packet_counts),
            "packet_errors": dict(self.packet_errors),
            "leaderboard": copy.deepcopy(self.leaderboard),
            "position_context": copy.deepcopy(self.latest.get("position_context")),
            "events": copy.deepcopy(self.events[-24:]),
            # T2.6: final classification rows once the session ends.
            "final_classification": copy.deepcopy(
                self.latest.get("final_classification")),
            # T4.1: full per-lap snapshots for the race model / debrief.
            "lap_snapshots": copy.deepcopy(self.lap_snapshots),
            **extra,
        }
