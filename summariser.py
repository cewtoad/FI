"""Summariser: turn a raw TelemetryState snapshot into readable structured output.

This is the layer that answers "what's going on" in plain language. It is pure
computation over the snapshot dict - no AI, no I/O. The AI/voice layer will
consume the same structured result later.

Output has two parts:
    - ``facts``: short labelled values, ready to print or feed to an LLM
    - ``notes``: human-readable observations ("tyre temps rising", "fuel deficit")
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from contracts import SuggestedAction
from race_model import TYRE_WEAR_LIMIT_DEFAULT
from timefmt import fmt_gap, fmt_gap_signed, fmt_ms, fmt_signed_ms

# Back-compat aliases (tests / callers that imported private helpers).
_fmt_ms = fmt_ms
_signed = fmt_signed_ms
_fmt_gap = fmt_gap
_fmt_gap_signed = fmt_gap_signed

# Named intents for AI / ask path fact slices (fuel / tyres / gaps / pit / weather).
INTENT_CORE: Tuple[str, ...] = (
    "lap", "total_laps", "position", "last_lap_time", "best_lap_time",
    "current_lap_time", "tyre_compound", "tyre_age_laps", "speed_kph",
    "laps_remaining",
)
INTENT_GROUPS: Dict[str, Tuple[str, ...]] = {
    "tyres": ("tyre", "stint", "pace"),
    "fuel": ("fuel", "pit", "stint"),
    "gaps": ("gap", "position"),
    "pit": ("pit", "fuel", "stint"),
    "weather": ("weather",),
    "pace": ("pace", "stint", "qualifying"),
}
INTENT_FLAT_PREFIX: Dict[str, Tuple[str, ...]] = {
    "tyres": ("tyre_",),
    "fuel": ("fuel_",),
    "gaps": (),
    "pit": ("fuel_",),
    "weather": (),
    "pace": (),
}
QUESTION_INTENT_HINTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("tyres", ("胎", "轮胎", "磨损", "胎温", "stint", "衰退")),
    ("gaps", ("前车", "后面", "落后", "差距", "追", "超", "名次", "位置", "gap")),
    ("pit", ("进站", "策略", "油", "窗口", "pit", "fuel", "plan")),
    ("fuel", ("油量", "剩油", "油耗", "fuel")),
    ("weather", ("天气", "雨", "weather", "rain")),
    ("pace", ("排位", "最快圈", "杆位", "quali", "圈速", "节奏")),
)


class Summariser:
    """Derives facts and notes from a snapshot dict."""

    # Tunable thresholds (T1.3: tyre threshold now read from config so it can
    # be calibrated against replay data without a code change).
    HOT_TYRE_C = 110
    RISING_TYRE_C = 6      # delta across recent laps considered "rising"
    FUEL_DEFICIT_LAPS = -0.2

    def __init__(self, config: Any = None) -> None:
        if config is None:
            try:
                from config import get_config
                config = get_config()
            except Exception:  # noqa: BLE001 - summariser must never hard-fail
                config = None
        self._cfg = config

    @property
    def hot_tyre_c(self) -> float:
        """Inner-tyre "too hot" threshold (default 110C; PLACEHOLDER, T1.3).

        NOTE: this default has NOT been calibrated against real replay data
        (see PLAN v2 stop point #6). Calibrate before trusting the warning.
        """
        if self._cfg is None:
            return self.HOT_TYRE_C
        try:
            return self._cfg.get_float("TYRE_HOT_INNER_C", self.HOT_TYRE_C)
        except Exception:  # noqa: BLE001
            return self.HOT_TYRE_C

    @property
    def wear_limit_pct(self) -> float:
        """Wear % considered high — same source as race_model's projected life.

        One knob (``TYRE_WEAR_LIMIT_PCT``) drives both the radio's tyre-life
        prediction and this note, so the two can never disagree.
        """
        try:
            return float(self._cfg.get_float("TYRE_WEAR_LIMIT_PCT",
                                             TYRE_WEAR_LIMIT_DEFAULT))
        except Exception:  # noqa: BLE001
            return TYRE_WEAR_LIMIT_DEFAULT

    @property
    def fuel_deficit_laps(self) -> float:
        """Surplus-laps value below which fuel is called a deficit."""
        try:
            return float(self._cfg.get_float("RADIO_FUEL_DEFICIT_LAPS",
                                             self.FUEL_DEFICIT_LAPS))
        except Exception:  # noqa: BLE001
            return self.FUEL_DEFICIT_LAPS

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
        current_lap = lap.get("current_lap_num")
        session = snap.get("session", {})
        total_laps = session.get("total_laps")
        facts: Dict[str, Any] = {
            "lap": current_lap,
            "total_laps": total_laps,
            "position": pos,
            "current_lap_time": _fmt_ms(lap.get("current_lap_time_ms")),
            "last_lap_time": _fmt_ms(lap.get("last_lap_time_ms")),
            "best_lap_time": _fmt_ms(trends.get("best_lap_ms")),
            "delta_to_best": _signed(delta.get("delta_ms")),
            "sector1": _fmt_ms(lap.get("sector1_ms")),
            "sector2": _fmt_ms(lap.get("sector2_ms")),
            "sector3": _fmt_ms(lap.get("sector3_ms")),
            "gap_to_front": "领跑" if pos == 1 else _fmt_gap_signed(lap.get("delta_to_car_in_front_ms")),
            "gap_to_leader": "领先全场" if pos == 1 else _fmt_gap_signed(lap.get("delta_to_race_leader_ms")),
            "session_time_left_s": session.get("session_time_left_s",
                                               (latest.get("session") or {}).get("session_time_left_s")),
            "speed_kph": car.get("speed_kph"),
            "gear": car.get("gear"),
            "tyre_compound": status.get("tyre_compound_actual"),
            "tyre_age_laps": status.get("tyres_age_laps"),
            "tyre_temp_c": car.get("tyres_surface_temp_c"),
            "fuel_kg": status.get("fuel_in_tank_kg"),
            "fuel_laps_left": status.get("fuel_remaining_laps"),
            "ers_energy_j": status.get("ers_store_energy_j"),
        }
        # T1.5: under 2026 regulations DRS is replaced by Active Aero / Overtake
        # mode, so never surface a DRS state there (it would be misleading).
        regs_2026 = bool(car2.get("regulations_2026"))
        if not regs_2026:
            facts["drs_allowed"] = status.get("drs_allowed")

        # Laps remaining (T1.2): only meaningful when the session has a lap
        # count. Includes the lap being driven (standard race-engineer usage:
        # "5 laps to go" while on lap N of M means M-N+1). The race model's
        # race_laps_remaining uses the SAME convention (and is NOT exposed
        # under fuel.* — tank range is the flat fact fuel_laps_left).
        if isinstance(total_laps, int) and total_laps > 0 \
                and isinstance(current_lap, int) and current_lap > 0:
            facts["laps_remaining"] = max(0, total_laps - current_lap + 1)

        # Rain probability as a flat fact: the local router answers
        # "降雨概率多少" from it without falling through to the LLM.
        s_meta = latest.get("session", {}) or {}
        if s_meta.get("rain_percentage") is not None:
            facts["rain_percentage"] = s_meta.get("rain_percentage")

        if fuel:
            facts["fuel_surplus_laps"] = fuel.get("surplus_laps")
            facts["fuel_rate_kg_per_lap"] = fuel.get("curr_fuel_rate_kg_per_lap")
            facts["predicted_final_fuel_kg"] = fuel.get("predicted_final_fuel_kg")
            # Same deficit threshold the radio rule uses, so the local fast
            # answer and the proactive radio can never disagree.
            facts["fuel_deficit_threshold_laps"] = self.fuel_deficit_laps

        if car2:
            facts["active_aero"] = car2.get("active_aero_mode")
            facts["overtake_available"] = car2.get("overtake_available")
            facts["overtake_active"] = car2.get("overtake_active")

        # Damage summary (only surface if anything is actually damaged).
        damage = latest.get("damage", {})
        if damage:
            if damage.get("has_significant_damage"):
                facts["damage_bodywork_max_pct"] = damage.get("worst_bodywork")
            if not regs_2026 and damage.get("drs_fault"):
                facts["drs_fault"] = True
            if damage.get("engine_blown") or damage.get("engine_seized"):
                facts["engine_critical"] = True
            if damage.get("ers_fault"):
                facts["ers_fault"] = True
            tw = [damage.get(k) for k in
                  ("tyre_wear_fl", "tyre_wear_fr", "tyre_wear_rl", "tyre_wear_rr")]
            if any(v is not None for v in tw):
                facts["tyre_wear_pct"] = [round(v, 1) if v is not None else None for v in tw]

        # T9: namespaced facts from the race model (flat keys kept for the
        # router; these add trend/window context for the LLM).
        self._add_race_model_facts(facts, snap, regs_2026)

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
        notes.extend(self._damage_notes(damage, regs_2026))
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

        notes = dedupe_notes(notes)
        trend_lines = race_model_trend_lines(snap)
        suggestions = build_suggested_actions(snap, facts)

        return {
            "facts": facts,
            "notes": notes,
            "lap_history": lap_history,
            "leaderboard": leaderboard,
            "recent_events": recent_events,
            "trend_lines": trend_lines,
            "suggested_actions": [s.to_dict() for s in suggestions],
        }

    # ------------------------------------------------------------- note rules

    def _add_race_model_facts(self, facts, snap, regs_2026) -> None:
        rm = snap.get("race_model")
        if not rm:
            return
        stint = rm.get("stint") or {}
        if stint:
            facts["stint.compound"] = stint.get("compound")
            facts["stint.laps"] = stint.get("laps")
            facts["stint.wear_pct"] = stint.get("wear_now_pct")
            facts["stint.wear_rate_pct_per_lap"] = stint.get("wear_rate_pct_per_lap")
            facts["pace.degradation_s_per_lap"] = stint.get("pace_degradation_s_per_lap")
        if rm.get("tyre_laps_to_limit") is not None:
            facts["stint.tyre_laps_to_limit"] = round(float(rm["tyre_laps_to_limit"]), 1)
        ahead = rm.get("ahead") or {}
        behind = rm.get("behind") or {}
        if ahead:
            facts["gap.ahead_ms"] = ahead.get("gap_ms_now")
            facts["gap.ahead_closing_ms_per_lap"] = ahead.get("closing_rate_ms_per_lap")
            if ahead.get("laps_to_1s") is not None:
                facts["gap.laps_to_1s"] = ahead.get("laps_to_1s")
        if behind:
            facts["gap.behind_ms"] = behind.get("gap_ms_now")
        pw = rm.get("pit_window") or {}
        if pw:
            facts["pit.window_state"] = pw.get("state")
            facts["pit.ideal_lap"] = pw.get("ideal_lap")
            facts["pit.latest_lap"] = pw.get("latest_lap")
            facts["pit.rejoin_position"] = pw.get("rejoin_position")
        # Race distance remaining lives only as flat facts["laps_remaining"]
        # (set above). Do NOT mirror race_model.race_laps_remaining under
        # fuel.* — that namespace is reserved for tank/oil range
        # (fuel_laps_left / fuel_surplus_laps / ...).
        if facts.get("laps_remaining") is None and rm.get("race_laps_remaining") is not None:
            facts["laps_remaining"] = rm.get("race_laps_remaining")
        if rm.get("rain_eta_min") is not None:
            facts["weather.rain_eta_min"] = rm.get("rain_eta_min")
        if rm.get("field_best_lap_ms") is not None:
            facts["qualifying.field_best"] = _fmt_ms(rm.get("field_best_lap_ms"))

    def _tyre_notes(self, car, status) -> List[str]:
        out: List[str] = []
        # T1.3: use the ~3s median inner temperature, not the instantaneous
        # surface temperature (which spikes in every brake zone and caused
        # false "tyre overheating" alarms).
        temps = car.get("tyres_inner_temp_median_c")
        hot_c = self.hot_tyre_c
        if temps:
            hot = [i for i, t in enumerate(temps) if t and t >= hot_c]
            if hot:
                pos = ",".join("FL FR RL RR".split()[i] for i in hot)
                out.append(f"胎温过高: {pos} 内温中位超过 {hot_c:g}C (当前 {temps})")
        age = status.get("tyres_age_laps")
        if isinstance(age, int) and age >= 15:
            out.append(f"轮胎已使用 {age} 圈，接近衰退区间")
        return out

    def _fuel_notes(self, fuel, status) -> List[str]:
        out: List[str] = []
        surplus = fuel.get("surplus_laps")
        if isinstance(surplus, (int, float)):
            threshold = self.fuel_deficit_laps
            if surplus < threshold:
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

    def _damage_notes(self, damage, regs_2026: bool = False) -> List[str]:
        out: List[str] = []
        if not damage:
            return out
        if damage.get("engine_blown"):
            out.append("警告: 引擎已损毁")
        if damage.get("engine_seized"):
            out.append("警告: 引擎过热抱死")
        # T1.5: DRS does not exist under 2026 regs; the equivalent system is
        # Active Aero / Overtake, so suppress the DRS fault wording there.
        if damage.get("drs_fault") and not regs_2026:
            out.append("DRS 系统故障")
        if damage.get("ers_fault"):
            out.append("ERS 故障")
        body = damage.get("worst_bodywork")
        if body is not None and body >= 60:
            out.append(f"车损严重: 最严重部件已损坏 {body}%")
        elif body is not None and body >= 20:
            out.append(f"有车损: 最严重部件 {body}%")
        # Only mention tyre wear when meaningful (same threshold the race
        # model uses for projected tyre life).
        tw = [damage.get(k) for k in
              ("tyre_wear_fl", "tyre_wear_fr", "tyre_wear_rl", "tyre_wear_rr")]
        tw = [v for v in tw if v is not None]
        if tw and max(tw) >= self.wear_limit_pct:
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


# --------------------------------------------------------- intent / trends / advise

def detect_intents(question: str) -> List[str]:
    """Map a free-text question to zero or more INTENT_GROUPS keys."""
    q = question or ""
    q_lower = q.lower()
    out: List[str] = []
    for intent, hints in QUESTION_INTENT_HINTS:
        matched = False
        for h in hints:
            if h.isascii():
                if h.lower() in q_lower:
                    matched = True
                    break
            elif h in q:
                matched = True
                break
        if matched and intent not in out:
            out.append(intent)
    return out


def slice_facts(facts: Dict[str, Any], intents: Sequence[str] | None = None,
                question: str | None = None) -> Dict[str, Any]:
    """Return the fact subset for the given intents (or inferred from question).

    Empty intent list / no match -> full facts (safe default for the LLM).
    """
    if not facts:
        return {}
    if intents is None:
        intents = detect_intents(question or "")
    intents = list(intents or [])
    if not intents:
        return dict(facts)
    prefixes: List[str] = []
    flat: List[str] = []
    for intent in intents:
        for g in INTENT_GROUPS.get(intent, ()):
            prefixes.append(f"{g}.")
        flat.extend(INTENT_FLAT_PREFIX.get(intent, ()))
    if not prefixes and not flat:
        return dict(facts)
    pref_t = tuple(prefixes)
    flat_t = tuple(flat)
    out: Dict[str, Any] = {}
    for k, v in facts.items():
        if k in INTENT_CORE:
            out[k] = v
        elif pref_t and k.startswith(pref_t):
            out[k] = v
        elif flat_t and k.startswith(flat_t):
            out[k] = v
    return out or dict(facts)


def dedupe_notes(notes: List[str]) -> List[str]:
    """Drop exact duplicates (whitespace-normalised); keep first spelling."""
    seen: set = set()
    out: List[str] = []
    for n in notes or []:
        key = (n or "").strip()
        if not key:
            continue
        norm = " ".join(key.split())
        if norm in seen:
            continue
        seen.add(norm)
        out.append(key)
    return out


def race_model_trend_lines(snap: Dict[str, Any]) -> List[str]:
    """One-liners from race_model for radio / ask (no raw dict dump)."""
    rm = snap.get("race_model") or {}
    if not rm:
        return []
    lines: List[str] = []
    stint = rm.get("stint") or {}
    if stint.get("pace_degradation_s_per_lap") is not None:
        deg = float(stint["pace_degradation_s_per_lap"])
        if abs(deg) >= 0.01:
            lines.append(f"胎面衰退约 {deg:.3f} 秒/圈")
    if rm.get("tyre_laps_to_limit") is not None:
        lines.append(
            f"轮胎预计还能跑 {float(rm['tyre_laps_to_limit']):.0f} 圈到磨损上限")
    ahead = rm.get("ahead") or {}
    if ahead.get("closing_rate_ms_per_lap") is not None:
        rate = float(ahead["closing_rate_ms_per_lap"])
        if abs(rate) >= 20:
            verb = "追近" if rate > 0 else "被拉开"
            lines.append(f"对前车每圈{verb}约 {abs(rate):.0f}ms")
        if ahead.get("laps_to_1s") is not None and float(ahead["laps_to_1s"]) <= 5:
            lines.append(f"约 {float(ahead['laps_to_1s']):.1f} 圈可进 1 秒区间")
    pw = rm.get("pit_window") or {}
    state = pw.get("state")
    if state and state not in ("unknown", "done", "not_open"):
        ideal = pw.get("ideal_lap")
        latest = pw.get("latest_lap")
        bit = f"进站窗口 {state}"
        if ideal is not None:
            bit += f"，理想第 {ideal} 圈"
        if latest is not None:
            bit += f"，最晚第 {latest} 圈"
        lines.append(bit)
    if rm.get("rain_eta_min") is not None:
        lines.append(f"预计约 {float(rm['rain_eta_min']):.0f} 分钟后降雨")
    if rm.get("field_best_lap_ms"):
        lines.append(f"场上最快圈 {fmt_ms(rm.get('field_best_lap_ms'))}")
    return lines


def build_suggested_actions(snap: Dict[str, Any],
                            facts: Optional[Dict[str, Any]] = None
                            ) -> List[SuggestedAction]:
    """Advise-only actions for HUD / radio. Never encodes a pressable binding."""
    facts = facts or {}
    actions: List[SuggestedAction] = []
    rm = snap.get("race_model") or {}
    pw = rm.get("pit_window") or {}
    state = pw.get("state")
    if state in ("open", "last_lap"):
        ideal = pw.get("ideal_lap")
        latest = pw.get("latest_lap")
        bits = []
        if ideal is not None:
            bits.append(f"理想第 {ideal} 圈")
        if latest is not None:
            bits.append(f"最晚第 {latest} 圈")
        detail = "，".join(bits) if bits else state
        actions.append(SuggestedAction(
            text=f"进站窗口已开（{detail}）；由你自行决定是否进站",
            suggested_key_name="进站确认",
        ))
    car2 = (snap.get("latest") or {}).get("car2") or {}
    ov_avail = facts.get("overtake_available")
    if ov_avail is None:
        ov_avail = car2.get("overtake_available")
    ov_active = facts.get("overtake_active")
    if ov_active is None:
        ov_active = car2.get("overtake_active")
    if ov_avail and not ov_active:
        actions.append(SuggestedAction(
            text="Overtake 可用；需要时由你自行开启",
            suggested_key_name="Overtake",
        ))
    ahead = rm.get("ahead") or {}
    gap_ms = ahead.get("gap_ms_now")
    if isinstance(gap_ms, (int, float)) and 0 < gap_ms < 1000:
        actions.append(SuggestedAction(
            text=f"已进入追击范围（前车差 {fmt_gap(int(gap_ms))}）",
            suggested_key_name="DRS/Active Aero",
        ))
    return actions


def summarise_for_ask(snap: Dict[str, Any], question: str = "",
                      config: Any = None) -> Dict[str, Any]:
    """Full summarise + intent-sliced facts for the LLM ask path."""
    summary = Summariser(config=config).summarise(snap)
    summary["facts"] = slice_facts(summary.get("facts") or {},
                                   question=question)
    return summary
