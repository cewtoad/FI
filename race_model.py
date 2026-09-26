"""Race model: turn lap samples into trends/predictions/windows (T4).

Pure local computation - no AI, no I/O. ``update(snapshot, now)`` is called by
the ticker at 2Hz and recomputes a ``RaceModelState`` from the snapshot's
``lap_snapshots`` list plus the current session/lap/leaderboard. The result is
merged back into snapshots via ``state.add_snapshot_provider`` (see app.py) so
the summariser and radio director can read ``snapshot["race_model"]``.

Design notes:
  * The heavy work (regressions over the whole stint) is cheap because lap
    samples only change once per lap. The 2Hz update is well under the 2ms
    budget; a perf test asserts this.
  * Degradation computed from lap times INCLUDES the fuel-burn effect; we do
    not try to separate it (documented approximation).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from typing import Any, Dict, List, Optional

from contracts import GapTrend, PitWindow, RaceModelState, Stint

_log = logging.getLogger("f1_tr.race_model")

TYRE_WEAR_LIMIT_DEFAULT = 70.0

# str(SafetyCarType.X) is the enum NAME (lib/f1_types/base_pkt.py __str__), e.g.
# "NO_SAFETY_CAR". Keep the spaced spellings for old fixtures / recordings.
_NO_SC = frozenset({"", "0", "NONE", "NO_SAFETY_CAR", "NO SAFETY CAR",
                    "FORMATION_LAP", "FORMATION LAP"})


def _sc_active(value) -> bool:
    return str(value or "").strip().upper() not in _NO_SC


def _linreg(xs: List[float], ys: List[float]):
    """Return (slope, intercept) via least squares; (0, mean) if degenerate."""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return 0.0, my
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = num / denom
    return slope, my - slope * mx


class RaceModel:
    """Computes stint/pace/gap/pit-window trends from lap snapshots."""

    def __init__(self, config: Any = None,
                 clock=time.monotonic) -> None:
        self._cfg = config
        self._clock = clock
        self._lock = threading.Lock()
        self.latest: RaceModelState = RaceModelState(upto_lap=0,
                                                     session_kind="unknown")
        # GapTrend sample history keyed by target car index.
        self._ahead_samples: List[tuple] = []   # (lap, gap_ms, car_index)
        self._behind_samples: List[tuple] = []
        # Pit-window latch: the game's pit_status is transient (NONE again once
        # out of the pits), so "done" must be remembered across ticks until the
        # window rotates to the next planned stop.
        self._pit_window_id: Optional[tuple] = None
        self._pit_window_stop0: Optional[int] = None

    # ---------------------------------------------------------------- config

    def _wear_limit(self) -> float:
        if self._cfg is None:
            return TYRE_WEAR_LIMIT_DEFAULT
        try:
            return self._cfg.get_float("TYRE_WEAR_LIMIT_PCT", TYRE_WEAR_LIMIT_DEFAULT)
        except Exception:  # noqa: BLE001
            return TYRE_WEAR_LIMIT_DEFAULT

    # ---------------------------------------------------------------- update

    def update(self, snapshot: Dict[str, Any], now: float) -> None:
        try:
            state = self._compute(snapshot)
        except Exception as e:  # noqa: BLE001 - never break the ticker
            _log.warning("race model update failed: %r", e)
            return
        with self._lock:
            self.latest = state

    def _compute(self, snap: Dict[str, Any]) -> RaceModelState:
        latest = snap.get("latest", {}) or {}
        # The real TelemetryState snapshot keeps the Session-packet fields under
        # latest.session; top-level "session" only has uid/type/track/total_laps.
        session = {**(snap.get("session") or {}), **(latest.get("session") or {})}
        lap = latest.get("lap", {}) or {}
        status = latest.get("status", {}) or {}
        fuel = snap.get("fuel", {}) or {}
        session_kind = session.get("session_kind") or "unknown"
        samples: List[Dict[str, Any]] = snap.get("lap_snapshots") or []
        upto = 0
        if samples:
            upto = max(int(s.get("lap_num") or 0) for s in samples)

        stint = self._compute_stint(samples)
        ahead = self._compute_gap(samples, "ahead", session.get("total_laps"))
        behind = self._compute_gap(samples, "behind", session.get("total_laps"))
        pit_window = self._compute_pit_window(session, lap)
        rain_eta = self._compute_rain_eta(session, self._rain_threshold())

        tyre_laps_to_limit = None
        if stint is not None and stint.projected_life_laps is not None:
            tyre_laps_to_limit = max(0.0, stint.projected_life_laps)

        fuel_laps_left = None
        surplus = fuel.get("surplus_laps")
        if isinstance(surplus, (int, float)) and session.get("total_laps"):
            cur = lap.get("current_lap_num")
            if isinstance(cur, int):
                # Same laps-remaining convention as summariser.laps_remaining
                # (total - cur + 1, includes the lap being driven): the LLM and
                # radio templates must never see two "laps left" numbers that
                # disagree by one.
                fuel_laps_left = max(0, session["total_laps"] - cur + 1)

        field_best, pole = self._field_best(snap)
        tt = latest.get("time_trial", {}) or {}
        tt_pb = tt.get("personal_best_ms")
        flags = {
            "safety_car": (session.get("safety_car_status") or ""),
            "safety_car_active": _sc_active(session.get("safety_car_status")),
            "player_in_yellow_zone": self._player_in_yellow(snap, session),
            "session_time_left_s": session.get("session_time_left_s"),
            # Persists the PB across ticks so the tt_new_pb rule can compare
            # against the previous tick instead of announcing the first PB it
            # ever sees as a "new record".
            "tt_pb_ms": tt_pb if isinstance(tt_pb, int) and tt_pb > 0 else None,
        }
        return RaceModelState(
            upto_lap=upto,
            session_kind=session_kind,
            stint=stint,
            ahead=ahead,
            behind=behind,
            pit_window=pit_window,
            tyre_laps_to_limit=tyre_laps_to_limit,
            fuel_laps_left=fuel_laps_left,
            rain_eta_min=rain_eta,
            field_best_lap_ms=field_best,
            pole_lap_ms=pole,
            flags=flags,
        )

    # ---------------------------------------------------------------- stint

    def _compute_stint(self, samples: List[Dict[str, Any]]) -> Optional[Stint]:
        if not samples:
            return None
        # New stint when the compound changes or tyre age resets.
        start = len(samples) - 1
        compound = samples[-1].get("tyre_compound")
        for i in range(len(samples) - 2, -1, -1):
            s = samples[i]
            s_nxt = samples[i + 1]
            age_now = s_nxt.get("tyre_age_laps")
            age_prev = s.get("tyre_age_laps")
            reset = (isinstance(age_now, int) and isinstance(age_prev, int)
                     and age_now <= age_prev)
            if s.get("tyre_compound") != compound or reset:
                break
            start = i
        stint_samples = samples[start:]
        laps = len(stint_samples)

        # Wear regression: exclude out-laps and safety-car laps.
        clean = [s for s in stint_samples
                 if not s.get("pit_this_lap") and not self._sc_lap(s)]
        wear_rate = 0.0
        wear_now = 0.0
        if clean:
            last_wear = clean[-1].get("tyre_wear_max_pct")
            wear_now = float(last_wear) if last_wear is not None else 0.0
            pts = [(float(s.get("tyre_age_laps") or 0), float(s["tyre_wear_max_pct"]))
                   for s in clean if s.get("tyre_wear_max_pct") is not None]
            if len(pts) >= 3:
                wear_rate, _ = _linreg([p[0] for p in pts], [p[1] for p in pts])
            elif len(pts) == 2:
                wear_rate = pts[1][1] - pts[0][1]
            wear_rate = max(0.0, wear_rate)

        # Pace degradation: valid, non-pit, non-SC laps.
        pace = [s for s in stint_samples
                if s.get("valid") and not s.get("pit_this_lap") and not self._sc_lap(s)]
        pace_rate = 0.0
        if len(pace) >= 3:
            xs = [float(s.get("lap_num") or 0) for s in pace]
            ys = [float(s["lap_time_ms"]) / 1000.0 for s in pace]
            pace_rate, _ = _linreg(xs, ys)

        # None = unknown (no measurable wear yet). Never emit inf: json.dumps
        # turns it into the invalid token `Infinity` (/api/state) and TTS says "inf".
        projected = ((self._wear_limit() - wear_now) / wear_rate
                     if wear_rate > 0 else None)
        return Stint(
            index=self._stint_index(samples, start),
            start_lap=int(stint_samples[0].get("lap_num") or 0),
            compound=str(compound or ""),
            laps=laps,
            wear_rate_pct_per_lap=round(wear_rate, 4),
            wear_now_pct=round(wear_now, 2),
            pace_degradation_s_per_lap=round(pace_rate, 4),
            projected_life_laps=(round(projected, 1) if projected is not None else None),
        )

    @staticmethod
    def _stint_index(samples: List[Dict[str, Any]], start: int) -> int:
        # Count compound changes before this stint as a rough index.
        count = 0
        prev = None
        for s in samples[:start]:
            c = s.get("tyre_compound")
            if prev is not None and c != prev:
                count += 1
            prev = c
        return count

    @staticmethod
    def _sc_lap(s: Dict[str, Any]) -> bool:
        return _sc_active(s.get("safety_car"))

    # ---------------------------------------------------------------- gaps

    def _compute_gap(self, samples: List[Dict[str, Any]],
                     side: str, max_laps: Any = None) -> Optional[GapTrend]:
        gap_key = f"gap_{side}_ms"
        idx_key = f"{side}_index"
        # Anchor on the CURRENT opponent (latest sample with data) and keep
        # only the samples since that opponent appeared (reset on change).
        series = []
        car_index = None
        for s in reversed(samples):
            idx = s.get(idx_key)
            gap = s.get(gap_key)
            if idx is None or gap is None:
                continue
            if car_index is None:
                car_index = idx
            if idx != car_index:
                break
            if self._sc_lap(s) or s.get("pit_this_lap"):
                continue
            series.append((float(s.get("lap_num") or 0), float(gap)))
        series.reverse()
        if not series:
            return None
        last_gap = int(series[-1][1])
        tail = series[-3:]
        if len(tail) >= 2:
            slope, _ = _linreg([p[0] for p in tail], [p[1] for p in tail])
        else:
            slope = 0.0
        laps_to_1s = None
        if slope < 0 and last_gap > 1000:
            laps_to_1s = (last_gap - 1000) / (-slope)
            # A near-zero negative slope (pure noise) must not produce an
            # absurd "catch up in 40000 laps" number for the LLM/radio.
            cap = float(max_laps) if isinstance(max_laps, (int, float)) \
                and max_laps > 0 else 100.0
            laps_to_1s = min(laps_to_1s, cap)
        return GapTrend(target_index=int(car_index), gap_ms_now=last_gap,
                        closing_rate_ms_per_lap=round(slope, 1),
                        laps_to_1s=(round(laps_to_1s, 1) if laps_to_1s is not None
                                    else None))

    # ------------------------------------------------------------ pit window

    def _compute_pit_window(self, session, lap) -> PitWindow:
        ideal = session.get("pit_window_ideal_lap")
        latest = session.get("pit_window_latest_lap")
        rejoin = session.get("pit_window_rejoin_position")
        cur = lap.get("current_lap_num")
        stops = lap.get("num_pit_stops")
        st = "unknown"
        if isinstance(ideal, int) and isinstance(cur, int) and ideal > 0:
            window_id = (ideal, latest)
            if window_id != self._pit_window_id:
                # Window rotated (next planned stop) or session changed:
                # re-arm the done-latch on the current stop count.
                self._pit_window_id = window_id
                self._pit_window_stop0 = stops if isinstance(stops, int) else None
            if cur < ideal:
                st = "not_open"
            elif isinstance(latest, int) and cur > latest:
                st = "missed"
            elif isinstance(latest, int) and cur == latest:
                st = "last_lap"
            else:
                st = "open"
            # pit_status lives in latest.lap (state.py _on_lap_data), not status.
            # It is transient — NONE again right after the stop — so "done"
            # must also latch on a completed stop: otherwise the state regresses
            # to open/missed the next lap and the radio re-announces a window
            # that was already used.
            if lap.get("pit_status") not in (None, "NONE") and cur >= ideal:
                st = "done"
            elif (isinstance(stops, int) and stops > 0
                  and self._pit_window_stop0 is not None
                  and stops > self._pit_window_stop0):
                st = "done"
        return PitWindow(state=st,
                         ideal_lap=ideal if isinstance(ideal, int) else 0,
                         latest_lap=latest if isinstance(latest, int) else 0,
                         rejoin_position=rejoin if isinstance(rejoin, int) else 0)

    # ---------------------------------------------------------------- weather

    def _rain_threshold(self) -> float:
        if self._cfg is None:
            return 50.0
        try:
            return self._cfg.get_float("RADIO_ALERT_RAIN_PCT", 50.0)
        except Exception:  # noqa: BLE001
            return 50.0

    @staticmethod
    def _compute_rain_eta(session, threshold: float = 50.0) -> Optional[float]:
        # First sample whose probability actually crosses the alert threshold:
        # a tiny early sample (e.g. 5%) must not mask a later 80% forecast.
        forecast = session.get("weather_forecast") or []
        for entry in forecast:
            rain = entry.get("rain_pct") or 0
            if rain >= threshold:
                return float(entry.get("time_offset_min") or 0)
        return None

    # -------------------------------------------------------------- field

    @staticmethod
    def _field_best(snap):
        rows = snap.get("leaderboard") or []
        bests = [r.get("best_lap_ms") for r in rows
                 if isinstance(r.get("best_lap_ms"), int) and r["best_lap_ms"] > 0]
        if not bests:
            return None, None
        best = min(bests)
        return best, best

    @staticmethod
    def _player_in_yellow(snap, session) -> bool:
        zones = session.get("marshal_yellow_zones") or []
        if not zones:
            return False
        lap = (snap.get("latest", {}) or {}).get("lap", {}) or {}
        dist = lap.get("lap_distance_m")
        total = session.get("track_length_m")
        if not isinstance(dist, (int, float)) or not isinstance(total, (int, float)) or total <= 0:
            return False
        frac = dist / total
        # In a yellow zone if within ~0.03 of a zone start.
        return any(abs(frac - z) <= 0.03 for z in zones)

    # ---------------------------------------------------------------- access

    def latest_dict(self) -> Dict[str, Any]:
        """Return the latest state as a JSON/dict-friendly structure."""
        with self._lock:
            return _state_to_dict(self.latest)


def _state_to_dict(state: RaceModelState) -> Dict[str, Any]:
    d = asdict(state)
    return d
