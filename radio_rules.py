"""Radio rules: turn race-model transitions into Alerts (T5.1/T5.2).

Each rule is a small pure function of a RuleCtx (previous + current race model,
snapshot, fresh events, config, name renderer). Rules never call an LLM - the
director renders their template and the speech arbiter speaks it.

The default rule set implements the race/quali/practice/time-trial tables.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

from contracts import (Alert, PRIORITY_P0, PRIORITY_P1, PRIORITY_P2)

import radio_templates as T


@dataclass
class RuleCtx:
    prev: Any                       # RaceModelState (may be None)
    curr: Any                       # RaceModelState
    snapshot: dict
    new_events: List[dict]
    now: float
    settings: Any                   # config.Config
    names: Any                      # names.NameRenderer


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    priority: int
    cooldown_s: float
    session_kinds: Tuple[str, ...]
    min_verbosity: str
    check: Callable[[RuleCtx], Optional[Alert]]


def _alert(rule: Rule, text: str, now: float, dedup: str = "",
           meta: Optional[dict] = None) -> Alert:
    return Alert(id=rule.id, category=rule.category, priority=rule.priority,
                 text=text, created_at=now, dedup_key=dedup or rule.id,
                 session_kinds=rule.session_kinds, meta=meta or {})


# ---------------------------------------------------------------- helpers

def _sc_status(model) -> str:
    if model is None:
        return ""
    return str((model.flags or {}).get("safety_car") or "")


def _sc_active(model) -> bool:
    return bool((model.flags or {}).get("safety_car_active")) if model else False


def _prev_sc(prev) -> str:
    if prev is None:
        return ""
    return str((prev.flags or {}).get("safety_car") or "")


def _session(ctx) -> dict:
    """Session fields: real snapshots keep them under latest.session."""
    snap = ctx.snapshot or {}
    return {**(snap.get("session") or {}),
            **((snap.get("latest") or {}).get("session") or {})}


def _lap_key(ctx, base: str) -> str:
    """Scope a transition/state dedup key to the current lap.

    A transition (safety car #2, a later pit window) must be able to announce
    again on a different lap, but the director otherwise suppresses a repeated
    dedup_key for the whole session.
    """
    lap = (ctx.snapshot.get("latest", {}) or {}).get("lap", {}) or {}
    n = lap.get("current_lap_num")
    return f"{base}_{n if isinstance(n, int) else 'x'}"


# ---------------------------------------------------------------- race P0

def _rule_sc_deployed(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["sc_deployed"]
    if not _sc_active(ctx.curr) or "VIRTUAL" in _sc_status(ctx.curr).upper():
        return None   # VSC has its own rule; do not announce it twice
    # Deploy transition: previous not active, current active.
    prev_active = bool((ctx.prev.flags or {}).get("safety_car_active")) if ctx.prev else False
    if prev_active:
        return None
    return _alert(rule, T.render("sc_deployed", name="安全车"), ctx.now,
                  _lap_key(ctx, "sc_deployed"))


def _rule_vsc_deployed(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["vsc_deployed"]
    cur = _sc_status(ctx.curr).upper()
    prev = _prev_sc(ctx.prev).upper()
    if "VIRTUAL" in cur and "VIRTUAL" not in prev:
        return _alert(rule, T.render("vsc_deployed"), ctx.now,
                      _lap_key(ctx, "vsc_deployed"))
    return None


def _rule_sc_ending(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["sc_ending"]
    for ev in ctx.new_events:
        if ev.get("kind") == "safety_car" and ev.get("event_type") in (1, 2, 3):
            return _alert(rule, T.render("sc_ending"), ctx.now,
                          f"sc_ending_{ev.get('seq')}")
    return None


def _rule_red_flag(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["red_flag"]
    for ev in ctx.new_events:
        if ev.get("kind") == "red_flag":
            return _alert(rule, T.render("red_flag"), ctx.now,
                          f"red_flag_{ev.get('seq')}")
    return None


def _rule_engine_failure(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["engine_failure"]
    damage = (ctx.snapshot.get("latest", {}) or {}).get("damage", {}) or {}
    if damage.get("engine_blown") or damage.get("engine_seized"):
        return _alert(rule, T.render("engine_failure"), ctx.now, "engine_failure")
    return None


def _rule_major_damage(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["major_damage"]
    damage = (ctx.snapshot.get("latest", {}) or {}).get("damage", {}) or {}
    body = damage.get("worst_bodywork")
    if not isinstance(body, (int, float)):
        return None
    # Fire on first crossing 20% and again at 60%.
    if 20 <= body < 60:
        return _alert(rule, T.render("major_damage", pct=int(body)), ctx.now,
                      "major_damage_20")
    if body >= 60:
        return _alert(rule, T.render("major_damage", pct=int(body)), ctx.now,
                      "major_damage_60")
    return None


def _rule_drs_fault(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["drs_fault"]
    damage = (ctx.snapshot.get("latest", {}) or {}).get("damage", {}) or {}
    if damage.get("drs_fault"):
        return _alert(rule, T.render("drs_fault"), ctx.now, "drs_fault")
    return None


def _rule_ers_fault(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["ers_fault"]
    damage = (ctx.snapshot.get("latest", {}) or {}).get("damage", {}) or {}
    if damage.get("ers_fault"):
        return _alert(rule, T.render("ers_fault"), ctx.now, "ers_fault")
    return None


def _rule_wrong_way(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["wrong_way"]
    car2 = (ctx.snapshot.get("latest", {}) or {}).get("car2", {}) or {}
    if car2.get("driving_wrong_way"):
        return _alert(rule, T.render("wrong_way"), ctx.now, "wrong_way")
    return None


def _rule_penalty_issued(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["penalty_issued"]
    for ev in ctx.new_events:
        if ev.get("kind") == "penalty" and ev.get("is_player"):
            secs = ev.get("penalty_time_s") or 0
            return _alert(rule, T.render("penalty_issued", seconds=secs), ctx.now,
                          f"penalty_issued_{ev.get('seq')}")
    return None


def _rule_player_retired(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["player_retired"]
    for ev in ctx.new_events:
        if ev.get("kind") == "retirement" and ev.get("is_player"):
            return _alert(rule, T.render("player_retired"), ctx.now, "player_retired")
    return None


# ---------------------------------------------------------------- race P1

def _window_state(model) -> str:
    if model is None or model.pit_window is None:
        return "unknown"
    return model.pit_window.state


def _rule_pit_window_open(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["pit_window_open"]
    if _window_state(ctx.prev) == "open" or _window_state(ctx.curr) != "open":
        return None
    w = ctx.curr.pit_window
    return _alert(rule, T.render("pit_window_open", ideal=w.ideal_lap,
                                 latest=w.latest_lap, rejoin=w.rejoin_position),
                  ctx.now, _lap_key(ctx, "pit_window_open"))


def _rule_pit_window_last(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["pit_window_last"]
    if _window_state(ctx.prev) == "last_lap" or _window_state(ctx.curr) != "last_lap":
        return None
    w = ctx.curr.pit_window
    return _alert(rule, T.render("pit_window_last", latest=w.latest_lap),
                  ctx.now, _lap_key(ctx, "pit_window_last"))


def _rule_pit_window_missed(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["pit_window_missed"]
    if _window_state(ctx.curr) != "missed" or _window_state(ctx.prev) == "missed":
        return None
    w = ctx.curr.pit_window
    return _alert(rule, T.render("pit_window_missed", latest=w.latest_lap),
                  ctx.now, _lap_key(ctx, "pit_window_missed"))


def _rule_pit_sc_opportunity(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["pit_sc_opportunity"]
    if not _sc_active(ctx.curr):
        return None
    w = ctx.curr.pit_window
    if w is None or w.state not in ("open", "last_lap"):
        return None
    return _alert(rule, T.render("pit_sc_opportunity", ideal=w.ideal_lap,
                                 rejoin=w.rejoin_position),
                  ctx.now, _lap_key(ctx, "pit_sc_opportunity"))


def _rule_fuel_deficit(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["fuel_deficit"]
    surplus = (ctx.snapshot.get("fuel") or {}).get("surplus_laps")
    if isinstance(surplus, (int, float)) and surplus < -0.2:
        return _alert(rule, T.render("fuel_deficit", laps=abs(surplus)), ctx.now,
                      "fuel_deficit")
    return None


def _rule_tyre_critical(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["tyre_critical"]
    laps = ctx.curr.tyre_laps_to_limit
    if laps is None:
        return None
    if laps <= 3:
        return _alert(rule, T.render("tyre_critical", laps=laps), ctx.now,
                      "tyre_critical")
    return None


def _rule_tyre_attention(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["tyre_attention"]
    laps = ctx.curr.tyre_laps_to_limit
    if laps is None or laps > 5 or laps <= 3:
        return None
    return _alert(rule, T.render("tyre_attention", laps=laps), ctx.now,
                  "tyre_attention")


def _rule_rain_incoming(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["rain_incoming"]
    eta = ctx.curr.rain_eta_min
    if eta is None or eta > 10:
        return None
    forecast = _session(ctx).get("weather_forecast") or []
    pct = 0
    for e in forecast:
        if e.get("time_offset_min") == eta:
            pct = e.get("rain_pct") or 0
    threshold = 50
    try:
        threshold = ctx.settings.get_int("RADIO_ALERT_RAIN_PCT", 50)
    except Exception:
        pass
    if pct < threshold:
        return None
    return _alert(rule, T.render("rain_incoming", eta=eta, pct=int(pct)), ctx.now,
                  "rain_incoming")


def _rule_track_limits_warning(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["track_limits_warning"]
    lap = (ctx.snapshot.get("latest", {}) or {}).get("lap", {}) or {}
    count = lap.get("corner_cutting_warnings")
    if isinstance(count, int) and count in (2, 3):
        return _alert(rule, T.render("track_limits_warning", count=count), ctx.now,
                      f"track_limits_{count}")
    return None


def _rule_unserved_penalty(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["unserved_penalty"]
    lap = (ctx.snapshot.get("latest", {}) or {}).get("lap", {}) or {}
    dt = lap.get("num_unserved_dt_pens") or 0
    sg = lap.get("num_unserved_sg_pens") or 0
    if dt or sg:
        kind = "停走" if sg else "通过"
        return _alert(rule, T.render("unserved_penalty", kind=kind), ctx.now,
                      f"unserved_{kind}")
    return None


def _rule_undercut_risk(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["undercut_risk"]
    behind = ctx.curr.behind
    if behind is None or behind.gap_ms_now > 3000:
        return None
    lap = (ctx.snapshot.get("latest", {}) or {}).get("lap", {}) or {}
    if lap.get("pit_status") == "PITTING":
        # The opponent in front pitting is an *opportunity*, handled elsewhere;
        # here we only flag the car behind us pitting (undercut risk).
        pass
    lb = ctx.snapshot.get("leaderboard") or []
    for r in lb:
        if r.get("car_index") == behind.target_index and r.get("pit_status") in ("PITTING", "IN_PIT_AREA"):
            # Use the leaderboard's driver name instead of the empty participant
            # map, which always degraded to "carN".
            name = r.get("driver") or ctx.names.name_from_index(behind.target_index, {})
            return _alert(rule, T.render("undercut_risk", name=name), ctx.now,
                          "undercut_risk")
    return None


def _rule_yellow_ahead(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["yellow_ahead"]
    if (ctx.curr.flags or {}).get("player_in_yellow_zone"):
        return _alert(rule, T.render("yellow_ahead", zone=""), ctx.now,
                      _lap_key(ctx, "yellow_ahead"))
    return None


# ---------------------------------------------------------------- race P2

def _rule_position_change(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["position_change"]
    for ev in ctx.new_events:
        if ev.get("kind") in ("position_up", "position_down", "field_overtake"):
            return _alert(rule, T.render("position_change", text=ev.get("text", "")),
                          ctx.now, f"pos_{ev.get('seq')}")
    return None


def _rule_fastest_lap_you(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["fastest_lap_you"]
    for ev in ctx.new_events:
        if ev.get("kind") == "fastest_lap" and ev.get("is_player"):
            ms = ev.get("lap_time_ms")
            t = _fmt_ms(ms)
            return _alert(rule, T.render("fastest_lap_you", time=t), ctx.now,
                          "fastest_lap_you")
    return None


def _rule_retirement_other(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["retirement_other"]
    for ev in ctx.new_events:
        if ev.get("kind") == "retirement" and not ev.get("is_player"):
            return _alert(rule, T.render("retirement_other", name=f"car{ev.get('vehicle_idx')}"),
                          ctx.now, f"retire_{ev.get('vehicle_idx')}")
    return None


def _rule_final_lap(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["final_lap"]
    session = ctx.snapshot.get("session", {}) or {}
    lap = (ctx.snapshot.get("latest", {}) or {}).get("lap", {}) or {}
    total = session.get("total_laps")
    cur = lap.get("current_lap_num")
    if isinstance(total, int) and isinstance(cur, int) and cur == total:
        # Fire once by dedup (director dedups by key across the whole session).
        return _alert(rule, T.render("final_lap"), ctx.now, "final_lap")
    return None


def _rule_chequered(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["chequered"]
    for ev in ctx.new_events:
        if ev.get("kind") == "chequered":
            pos = (ctx.curr.flags or {}).get("final_position")
            lap = (ctx.snapshot.get("latest", {}) or {}).get("lap", {}) or {}
            pos = pos or lap.get("car_position") or 0
            return _alert(rule, T.render("chequered", pos=pos), ctx.now, "chequered")
    return None


def _rule_gap_report(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["gap_report"]
    mode = "every_lap"
    try:
        mode = ctx.settings.get("RADIO_GAP_MODE", "every_lap") or "every_lap"
    except Exception:
        pass
    if mode == "off":
        return None
    lap = (ctx.snapshot.get("latest", {}) or {}).get("lap", {}) or {}
    cur = lap.get("current_lap_num")
    ahead = ctx.curr.ahead
    behind = ctx.curr.behind
    if ahead is None and behind is None:
        return None
    if mode == "every_n_laps":
        try:
            n = ctx.settings.get_int("RADIO_GAP_EVERY_N", 3)
        except Exception:
            n = 3
        if not isinstance(cur, int) or (n and cur % n != 0):
            return None
    text = T.render("gap_report",
                    ahead=_fmt_gap(ahead.gap_ms_now) if ahead else "-",
                    behind=_fmt_gap(behind.gap_ms_now) if behind else "-")
    return _alert(rule, text, ctx.now, f"gap_{cur}")


def _rule_lap_summary_chatty(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["lap_summary_chatty"]
    lap = (ctx.snapshot.get("latest", {}) or {}).get("lap", {}) or {}
    cur = lap.get("current_lap_num")
    if not isinstance(cur, int) or cur < 2:
        return None
    return _alert(rule, T.render("lap_summary_chatty", lap=cur - 1,
                                 last=_fmt_ms(lap.get("last_lap_time_ms")),
                                 pos=lap.get("car_position") or 0),
                  ctx.now, f"lap_summary_{cur}")


# ---------------------------------------------------------------- quali

def _rule_quali_time(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["quali_time"]
    left = _session(ctx).get("session_time_left_s")
    if not isinstance(left, (int, float)):
        return None
    # Smallest mark first: at 50s left say "1 分钟", not "5 分钟".
    for mark, tid in ((60, "quali_time_60"), (120, "quali_time_120"), (300, "quali_time_300")):
        if left <= mark:
            return _alert(rule, T.render(tid), ctx.now, tid)
    return None


def _rule_quali_lap_done(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["quali_lap_done"]
    # Fires once per completed valid lap (dedup by lap number via director).
    snap = ctx.snapshot
    records = (snap.get("trends") or {}).get("lap_records") or []
    if not records:
        return None
    last = records[-1]
    if not last.get("valid"):
        return None
    if ctx.prev is not None and ctx.prev.upto_lap == ctx.curr.upto_lap:
        return None
    lap = (snap.get("latest", {}) or {}).get("lap", {}) or {}
    best = ctx.curr.field_best_lap_ms
    delta = "-"
    if best and last.get("lap_time_ms"):
        d = last["lap_time_ms"] - best
        delta = f"+{d/1000:.3f}s" if d >= 0 else f"{d/1000:.3f}s"
    return _alert(rule, T.render("quali_lap_done", lap=last.get("lap_num"),
                                 time=_fmt_ms(last.get("lap_time_ms")),
                                 pos=lap.get("car_position") or 0, delta=delta),
                  ctx.now, f"quali_lap_{last.get('lap_num')}")


# ---------------------------------------------------------------- practice

def _rule_practice_long_run(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["practice_long_run"]
    stint = ctx.curr.stint
    if stint is None or stint.laps < 5:
        return None
    if ctx.prev is not None and ctx.prev.stint is not None \
            and ctx.prev.stint.laps == stint.laps:
        return None
    avg = "-"
    pace = ctx.curr.flags.get("stint_avg_lap_s") if ctx.curr.flags else None
    if pace:
        avg = f"{pace:.3f}s"
    return _alert(rule, T.render("practice_long_run", avg=avg,
                                 deg=stint.pace_degradation_s_per_lap),
                  ctx.now, f"practice_long_{ctx.curr.upto_lap}")


# ---------------------------------------------------------------- time trial

def _rule_tt_new_pb(ctx: RuleCtx) -> Optional[Alert]:
    rule = _RULES["tt_new_pb"]
    tt = (ctx.snapshot.get("latest", {}) or {}).get("time_trial", {}) or {}
    pb = tt.get("personal_best_ms")
    prev_best = None
    if ctx.prev is not None and isinstance(ctx.prev.flags, dict):
        prev_best = ctx.prev.flags.get("tt_pb_ms")
    if isinstance(pb, int) and pb > 0 and (prev_best is None or pb < prev_best):
        return _alert(rule, T.render("tt_new_pb", time=_fmt_ms(pb)), ctx.now,
                      f"tt_pb_{pb}")
    return None


# ---------------------------------------------------------------- format

def _fmt_ms(ms) -> str:
    if not isinstance(ms, int) or ms <= 0:
        return "-"
    m, rem = divmod(ms, 60000)
    s, milli = divmod(rem, 1000)
    return f"{m}:{s:02d}.{milli:03d}" if m else f"{s}.{milli:03d}"


def _fmt_gap(ms) -> str:
    if not isinstance(ms, (int, float)) or ms <= 0:
        return "-"
    return f"{ms/1000:.1f}s"


# ---------------------------------------------------------------- registry

_P0_RACE = ("sc_deployed", "vsc_deployed", "sc_ending", "red_flag",
            "engine_failure", "major_damage", "drs_fault", "ers_fault",
            "wrong_way", "penalty_issued", "player_retired")
_P1_RACE = ("pit_window_open", "pit_window_last", "pit_window_missed",
            "pit_sc_opportunity", "fuel_deficit", "tyre_critical",
            "tyre_attention", "rain_incoming", "track_limits_warning",
            "unserved_penalty", "undercut_risk", "yellow_ahead")
_P2_RACE = ("position_change", "fastest_lap_you", "gap_report",
            "lap_summary_chatty", "retirement_other", "final_lap", "chequered")


def build_default_rules() -> List[Rule]:
    rules: List[Rule] = []
    race_kinds = ("race",)

    def add(rid, cat, prio, cd, kinds, verbosity, fn, cooldowns=None):
        r = Rule(id=rid, category=cat, priority=prio, cooldown_s=cd,
                 session_kinds=kinds, min_verbosity=verbosity, check=fn)
        _RULES[rid] = r
        rules.append(r)

    def register(rid, cat, prio, cd, kinds, verbosity):
        """Look up ``_rule_<rid>`` and fail loudly if a declared id has no
        implementation (a rename used to silently drop the rule from the set)."""
        fn = globals().get(f"_rule_{rid}")
        if fn is None:
            raise RuntimeError(f"no rule function for id {rid!r}")
        add(rid, cat, prio, cd, kinds, verbosity, fn)

    # P0 - always on, all session kinds
    for rid in _P0_RACE:
        register(rid, "p0", PRIORITY_P0, 30.0, (), "minimal")
    # P1
    for rid in _P1_RACE:
        kinds = race_kinds if rid.startswith(("pit_", "fuel_", "undercut", "yellow", "track")) else ()
        register(rid, "p1", PRIORITY_P1, 15.0, kinds,
                 "minimal" if rid.startswith("pit_") else "normal")
    # P2
    for rid in _P2_RACE:
        register(rid, "p2", PRIORITY_P2, 10.0, race_kinds, "chatty")
    # quali
    for rid in ("quali_time", "quali_lap_done"):
        register(rid, "p2", PRIORITY_P2, 5.0, ("qualifying",), "normal")
    # practice
    add("practice_long_run", "p2", PRIORITY_P2, 30.0, ("practice",), "normal",
        _rule_practice_long_run)
    # time trial
    add("tt_new_pb", "p2", PRIORITY_P2, 5.0, ("time_trial",), "normal",
        _rule_tt_new_pb)
    return rules


_RULES: dict = {}
