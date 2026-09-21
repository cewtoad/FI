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

import time
from typing import Any, Dict, List, Optional

from lib.delta import LapDeltaManager
from lib.f1_types import (F1PacketType, F1Utils, LapHistoryData,
                          PacketEventData)
from lib.fuel_rate_recommender import FuelRateRecommender, FuelRemainingPerLap
from lib.rolling_history import RollingHistory
from lib.fuel_rate_recommender import FuelRateRecommender, FuelRemainingPerLap
from lib.rolling_history import RollingHistory


class TelemetryState:
    """Aggregates parsed packets for the player car into a queryable snapshot."""

    # How many laps of history to keep for trend answers.
    HISTORY_LAPS = 20
    # Minimum fuel required to finish (safety margin, kg). Adjust later via config.
    MIN_FUEL_KG = 1.5
    # After a player-involved official OVERTAKE event, a leaderboard position
    # diff in the same direction within this window is treated as the same
    # overtake (already reported) and not recorded again.
    OFFICIAL_EVENT_DEDUP_S = 8.0

    def __init__(self, error_logger: Optional[Any] = None) -> None:
        self._error_logger = error_logger
        self.session_uid: Optional[int] = None
        self.session_started: bool = False

        # Identity / session meta
        self.session_type: Optional[str] = None
        self.track_id: Optional[Any] = None
        self.total_laps: Optional[int] = None
        self.player_car_index: int = 0

        # Latest snapshot values (player car only)
        self.latest: Dict[str, Any] = {}
        self.packet_counts: Dict[str, int] = {}
        self.packet_errors: Dict[str, int] = {}

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

        # Event tracking (position changes etc.) - fed to the AI as "what just
        # happened" so it can answer overtake questions with authority.
        self._last_position: Optional[int] = None
        self.events: list = []          # recent notable events, newest last
        self._event_seq: int = 0

        # Spectator mode: when spectating, the car we care about is the one
        # being watched (spectatorCarIndex), not playerCarIndex.
        self.is_spectating: bool = False
        self.spectator_car_index: Optional[int] = None

        # Dedup between official OVERTAKE events and leaderboard position
        # diffs: ("up"|"down", expiry monotonic time) set when an official
        # player-involved overtake is recorded, consumed by the next diff.
        self._pending_diff_event: Optional[str] = None
        self._pending_diff_until: float = 0.0

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
        self._participants = {}
        self._all_car_status = None
        self.leaderboard = []
        self._last_position = None
        self.events = []
        self._event_seq = 0
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
        try:
            self._dispatch(packet)
        except Exception as e:  # noqa: BLE001 - intentionally broad
            pid = getattr(getattr(packet, "m_header", None), "m_packetId", "?")
            key = f"__ERROR__{pid}"
            self.packet_errors[key] = self.packet_errors.get(key, 0) + 1
            if self._error_logger is not None:
                self._error_logger.warning("state.process failed for %s: %r", pid, e)

    def _dispatch(self, packet) -> None:
        header = packet.m_header
        self.note_header(header)
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
        self.latest["session"] = {
            "session_type": str(packet.m_sessionType),
            "track_id": str(packet.m_trackId),
            "track_length_m": getattr(packet, "m_trackLength", None),
            "total_laps": getattr(packet, "m_totalLaps", None),
            "session_time_left_s": getattr(packet, "m_sessionTimeLeft", None),
            "air_temp_c": getattr(packet, "m_airTemperature", None),
            "track_temp_c": getattr(packet, "m_trackTemperature", None),
            "rain_percentage": getattr(packet, "m_rainPercentage", None),
            "weather": str(weather) if weather is not None else None,
            "safety_car_status": str(getattr(packet, "m_safetyCarStatus", "")),
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
        }

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
        # Fuel for the finished lap is ingested lazily from car-status samples.

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

    @staticmethod
    def _combine(ms_part: int, min_part: int) -> int:
        if not ms_part and not min_part:
            return 0
        return (min_part or 0) * 60000 + (ms_part or 0)

    # ----------------------------------------------------------------- events

    def _on_event(self, packet) -> None:
        """Handle EVENT packets.

        Only OVERTAKE (OVTK) is consumed so far. It is the authoritative
        source for "who overtook whom" (both vehicle indices come straight
        from the game), so it takes priority over the leaderboard position
        diff, which stays as a fallback for when no official event arrives.
        """
        code = getattr(packet, "m_eventCode", None)
        if code != PacketEventData.EventPacketType.OVERTAKE:
            return
        details = getattr(packet, "mEventDetails", None)
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
        self._event_seq += 1
        self.events.append({
            "seq": self._event_seq,
            "kind": kind,
            "from": overtaker,
            "to": overtaken,
            "text": text,
        })
        self.events = self.events[-20:]

    def _arm_diff_dedup(self, direction: str) -> None:
        """Remember that an official player-involved overtake was just recorded,
        so the next matching leaderboard diff is not double-reported."""
        self._pending_diff_event = direction
        self._pending_diff_until = time.monotonic() + self.OFFICIAL_EVENT_DEDUP_S

    def _driver_name(self, idx: int) -> str:
        pinfo = self._participants.get(idx)
        name = (pinfo or {}).get("name")
        return name or f"car{idx}"

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
                "last_lap_ms": lap.m_lastLapTimeInMS,
                "pit_status": str(lap.m_pitStatus),
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
        player_row = next((r for r in rows if r["is_player"]), None)
        if player_row:
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
            if direction == pending and time.monotonic() <= expiry:
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
        self._event_seq += 1
        self.events.append({
            "seq": self._event_seq,
            "kind": kind,
            "from": old_pos,
            "to": new_pos,
            "text": text,
        })
        # Keep only the recent tail to bound memory / prompt size.
        self.events = self.events[-20:]

    # -------------------------------------------------------------- snapshot

    def snapshot(self) -> Dict[str, Any]:
        """Return the current state plus analyzer results as a plain dict."""
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

        return {
            "session": {
                "session_uid": self.session_uid,
                "session_type": self.session_type,
                "track_id": str(self.track_id) if self.track_id is not None else None,
                "total_laps": self.total_laps,
                "player_car_index": self.player_car_index,
            },
            "latest": self.latest,
            "delta": None if delta is None else {
                "delta_ms": delta.delta_ms,
                "best_lap_num": delta.best_lap_num,
                "distance_m": delta.distance_m,
            },
            "fuel": fuel,
            "trends": {
                "lap_times_ms": self.lap_times_ms.values(),
                "best_lap_ms": self._best_lap_ms,
                # All completed laps incl. invalid ones (each {lap_num,
                # lap_time_ms, valid}); lap_times_ms above is valid-only.
                "lap_records": self.lap_records.values(),
            },
            "packet_counts": dict(self.packet_counts),
            "packet_errors": dict(self.packet_errors),
            "leaderboard": self.leaderboard,
            "position_context": self.latest.get("position_context"),
            "events": self.events[-6:],
        }
