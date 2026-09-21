"""Summariser: turn a raw TelemetryState snapshot into readable structured output.

This is the layer that answers "what's going on" in plain language. It is pure
computation over the snapshot dict - no AI, no I/O. The AI/voice layer will
consume the same structured result later.

Output has two parts:
    - ``facts``: short labelled values, ready to print or feed to an LLM
    - ``notes``: human-readable observations ("tyre temps rising", "fuel deficit")
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _fmt_ms(ms: Optional[int]) -> str:
    if ms is None or ms <= 0:
        return "-"
    minutes = ms // 60000
    rem = ms % 60000
    seconds = rem // 1000
    millis = rem % 1000
    if minutes:
        return f"{minutes}:{seconds:02d}.{millis:03d}"
    return f"{seconds}.{millis:03d}"


def _signed(ms: Optional[int]) -> str:
    if ms is None:
        return "-"
    return f"+{ms}ms" if ms >= 0 else f"{ms}ms"


def _fmt_gap(ms: Optional[int]) -> str:
    if not ms or ms <= 0:
        return "-"
    return f"+{ms/1000:.3f}s"


class Summariser:
    """Derives facts and notes from a snapshot dict."""

    # Tunable thresholds (move to config later).
    HOT_TYRE_C = 110
    RISING_TYRE_C = 6      # delta across recent laps considered "rising"
    FUEL_DEFICIT_LAPS = -0.2

    def summarise(self, snap: Dict[str, Any]) -> Dict[str, Any]:
        latest = snap.get("latest", {})
        lap = latest.get("lap", {})
        car = latest.get("car", {})
        car2 = latest.get("car2", {})
        status = latest.get("status", {})
        delta = snap.get("delta") or {}
        fuel = snap.get("fuel") or {}
        trends = snap.get("trends", {})

        pos = lap.get("car_position")
        facts: Dict[str, Any] = {
            "lap": lap.get("current_lap_num"),
            "position": pos,
            "current_lap_time": _fmt_ms(lap.get("current_lap_time_ms")),
            "last_lap_time": _fmt_ms(lap.get("last_lap_time_ms")),
            "best_lap_time": _fmt_ms(trends.get("best_lap_ms")),
            "delta_to_best": _signed(delta.get("delta_ms")),
            "sector1": _fmt_ms(lap.get("sector1_ms")),
            "sector2": _fmt_ms(lap.get("sector2_ms")),
            "sector3": _fmt_ms(lap.get("sector3_ms")),
            "gap_to_front": "领跑" if pos == 1 else _fmt_ms(lap.get("delta_to_car_in_front_ms")),
            "gap_to_leader": "领跑" if pos == 1 else _fmt_ms(lap.get("delta_to_race_leader_ms")),
            "speed_kph": car.get("speed_kph"),
            "gear": car.get("gear"),
            "tyre_compound": status.get("tyre_compound_actual"),
            "tyre_age_laps": status.get("tyres_age_laps"),
            "tyre_temp_c": car.get("tyres_surface_temp_c"),
            "fuel_kg": status.get("fuel_in_tank_kg"),
            "fuel_laps_left": status.get("fuel_remaining_laps"),
            "ers_energy_j": status.get("ers_store_energy_j"),
            "drs_allowed": status.get("drs_allowed"),
        }

        if fuel:
            facts["fuel_surplus_laps"] = fuel.get("surplus_laps")
            facts["fuel_rate_kg_per_lap"] = fuel.get("curr_fuel_rate_kg_per_lap")
            facts["predicted_final_fuel_kg"] = fuel.get("predicted_final_fuel_kg")

        if car2:
            facts["active_aero"] = car2.get("active_aero_mode")
            facts["overtake_available"] = car2.get("overtake_available")
            facts["overtake_active"] = car2.get("overtake_active")

        # Damage summary (only surface if anything is actually damaged).
        damage = latest.get("damage", {})
        if damage:
            if damage.get("has_significant_damage"):
                facts["damage_bodywork_max_pct"] = damage.get("worst_bodywork")
            if damage.get("drs_fault"):
                facts["drs_fault"] = True
            if damage.get("engine_blown") or damage.get("engine_seized"):
                facts["engine_critical"] = True
            if damage.get("ers_fault"):
                facts["ers_fault"] = True
            tw = [damage.get(k) for k in
                  ("tyre_wear_fl", "tyre_wear_fr", "tyre_wear_rl", "tyre_wear_rr")]
            if any(v is not None for v in tw):
                facts["tyre_wear_pct"] = [round(v, 1) if v is not None else None for v in tw]

        # Pit status.
        if lap.get("pit_status") and lap.get("pit_status") != "NONE":
            facts["pit_status"] = lap.get("pit_status")
        facts["pit_stops"] = lap.get("num_pit_stops")
        if status.get("pit_limiter"):
            facts["pit_limiter_on"] = True

        # Battle context: who is directly ahead/behind and by how much.
        posctx = snap.get("position_context") or {}
        ahead = posctx.get("ahead")
        behind = posctx.get("behind")
        if ahead:
            facts["car_ahead"] = f"{ahead['driver']} (P{ahead['position']}, {_fmt_gap(ahead['gap_ms'])})"
        if behind:
            facts["car_behind"] = f"{behind['driver']} (P{behind['position']})"

        leaderboard = snap.get("leaderboard") or []

        notes: List[str] = []
        notes.extend(self._tyre_notes(car, status))
        notes.extend(self._fuel_notes(fuel, status))
        notes.extend(self._pace_notes(delta, trends))
        notes.extend(self._session_notes(latest))
        notes.extend(self._aero_notes(car2))
        notes.extend(self._battle_notes(pos, ahead, behind))
        notes.extend(self._damage_notes(damage))
        notes.extend(self._pit_notes(lap, status))

        # Recent position-change events (authoritative "what just happened").
        events = snap.get("events") or []
        recent_events = [e.get("text") for e in events if e.get("text")]

        # Lap history: show every completed lap; invalid ones (cut / off-track)
        # stay visible but are marked. Falls back to the valid-only trend for
        # snapshots that predate lap_records.
        records = trends.get("lap_records") or []
        if records:
            lap_history = [
                _fmt_ms(r.get("lap_time_ms")) + ("" if r.get("valid") else "(无效)")
                for r in records
            ]
        else:
            lap_history = [_fmt_ms(x) for x in trends.get("lap_times_ms", [])]

        return {
            "facts": facts,
            "notes": notes,
            "lap_history": lap_history,
            "leaderboard": leaderboard,
            "recent_events": recent_events,
        }

    # ------------------------------------------------------------- note rules

    def _tyre_notes(self, car, status) -> List[str]:
        out: List[str] = []
        temps = car.get("tyres_surface_temp_c") or []
        if temps:
            hot = [i for i, t in enumerate(temps) if t and t >= self.HOT_TYRE_C]
            if hot:
                pos = ",".join("FL FR RL RR".split()[i] for i in hot)
                out.append(f"胎温过高: {pos} 超过 {self.HOT_TYRE_C}C (当前 {temps})")
        age = status.get("tyres_age_laps")
        if isinstance(age, int) and age >= 15:
            out.append(f"轮胎已使用 {age} 圈，接近衰退区间")
        return out

    def _fuel_notes(self, fuel, status) -> List[str]:
        out: List[str] = []
        surplus = fuel.get("surplus_laps")
        if isinstance(surplus, (int, float)):
            if surplus < self.FUEL_DEFICIT_LAPS:
                out.append(f"油量不足: 按当前消耗完赛缺 {abs(surplus):.2f} 圈")
            elif surplus > 1.0:
                out.append(f"油量富余: 可多用 {surplus:.2f} 圈，考虑推进")
        return out

    def _pace_notes(self, delta, trends) -> List[str]:
        out: List[str] = []
        d = delta.get("delta_ms")
        if isinstance(d, int):
            if d > 300:
                out.append(f"当前圈比最快圈慢 {d}ms，节奏偏慢")
            elif d < -100:
                out.append(f"当前圈比最快圈快 {abs(d)}ms，节奏很好")
        laps = trends.get("lap_times_ms") or []
        if len(laps) >= 2:
            if laps[-1] > laps[-2]:
                out.append(f"上圈比前圈慢 {laps[-1] - laps[-2]}ms，可能轮胎衰退或犯错")
        return out

    def _session_notes(self, latest) -> List[str]:
        out: List[str] = []
        s = latest.get("session", {})
        if s.get("rain_percentage"):
            out.append(f"降雨概率 {s['rain_percentage']}%，注意天气变化")
        return out

    def _aero_notes(self, car2) -> List[str]:
        out: List[str] = []
        if not car2:
            return out
        if car2.get("overtake_active"):
            out.append("Overtake 模式已激活")
        elif car2.get("overtake_available"):
            out.append("Overtake 模式可用")
        if car2.get("driving_wrong_way"):
            out.append("警告: 正在逆行")
        return out

    def _battle_notes(self, pos, ahead, behind) -> List[str]:
        out: List[str] = []
        if ahead:
            gap = ahead.get("gap_ms") or 0
            out.append(f"前车 {ahead['driver']} (P{ahead['position']})，差 {_fmt_gap(gap)}")
            if 0 < gap < 1000:
                out.append("已进入追击/超车范围（1秒内）")
        if behind and isinstance(pos, int) and pos > 1:
            out.append(f"后车 {behind['driver']} (P{behind['position']})")
        return out

    def _damage_notes(self, damage) -> List[str]:
        out: List[str] = []
        if not damage:
            return out
        if damage.get("engine_blown"):
            out.append("警告: 引擎已损毁")
        if damage.get("engine_seized"):
            out.append("警告: 引擎过热抱死")
        if damage.get("drs_fault"):
            out.append("DRS 系统故障")
        if damage.get("ers_fault"):
            out.append("ERS 故障")
        body = damage.get("worst_bodywork")
        if body is not None and body >= 60:
            out.append(f"车损严重: 最严重部件已损坏 {body}%")
        elif body is not None and body >= 20:
            out.append(f"有车损: 最严重部件 {body}%")
        # Only mention tyre wear when meaningful.
        tw = [damage.get(k) for k in
              ("tyre_wear_fl", "tyre_wear_fr", "tyre_wear_rl", "tyre_wear_rr")]
        tw = [v for v in tw if v is not None]
        if tw and max(tw) >= 70:
            out.append(f"轮胎磨损偏高: 最高 {round(max(tw))}%")
        return out

    def _pit_notes(self, lap, status) -> List[str]:
        out: List[str] = []
        pit = lap.get("pit_status")
        if pit and pit != "NONE":
            out.append(f"进站状态: {pit}")
        if status.get("pit_limiter"):
            out.append("限速器已开启（在维修区）")
        stops = lap.get("num_pit_stops")
        if isinstance(stops, int) and stops > 0:
            out.append(f"已进站 {stops} 次")
        return out
