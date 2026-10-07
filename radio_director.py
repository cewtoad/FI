"""Radio director: rule engine + gating + dedup/cooling (T5).

``tick(snapshot, now)`` runs the rules against the current race model, filters
by verbosity/quiet/cooldowns/per-lap cap/global min-gap, dedups by key, and
emits Alerts to the configured sink (voice arbiter, web AlertLog, and the
recorder). It NEVER calls an LLM.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from contracts import Alert, PRIORITY_P0, PRIORITY_P1
import names as names_mod
from radio_rules import Rule, RuleCtx

_log = logging.getLogger("f1_tr.radio")

_VERBOSITY_RANK = {"minimal": 0, "normal": 1, "chatty": 2}

# Per-lap alert caps by verbosity.
_PER_LAP_CAP = {"minimal": 1, "normal": 3, "chatty": 6}


class RadioDirector:
    def __init__(self, rules: List[Rule],
                 alert_sink: Optional[Callable[[Alert], None]] = None,
                 config: Any = None, clock=None,
                 name_renderer: Optional[names_mod.NameRenderer] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        import time as _time
        self.rules = rules
        self.sink = alert_sink
        self._cfg = config
        self._clock = clock or _time.monotonic
        self._log = logger or _log
        self.names = name_renderer
        self._lock = threading.Lock()
        self._last_fired: Dict[str, float] = {}
        self._fired_keys: Dict[str, float] = {}     # dedup_key -> last time
        self._last_event_seq = 0
        self._alerts_this_lap = 0
        self._lap_marker = None
        self._session_uid: Any = None
        self._snap_lap: dict = {}
        # -inf, not 0.0: with a clock that starts near 0 (tests, fresh boot of
        # an injected clock) the first alerts were swallowed by the min-gap.
        self._last_alert_at = float("-inf")
        self.total_alerts = 0
        # Persist last RaceModelState between ticks (for transitions).
        self._prev_model = None
        # Cache the name renderer: building one reloads driver_names.json from
        # disk, and the old code rebuilt it on every 2Hz tick.
        self._name_renderer = name_renderer

    # ---------------------------------------------------------------- config

    def _cfg_get(self, key, default):
        if self._cfg is None:
            return default
        try:
            return self._cfg.get(key, "") or default
        except Exception:
            return default

    def _cfg_bool(self, key, default):
        if self._cfg is None:
            return default
        try:
            return self._cfg.get_bool(key, default)
        except Exception:
            return default

    def _verbosity(self) -> str:
        v = str(self._cfg_get("RADIO_VERBOSITY", "chatty")).lower()
        return v if v in _VERBOSITY_RANK else "chatty"

    def _enabled(self) -> bool:
        return self._cfg_bool("RADIO_ENABLE", True)

    def _quiet_active(self) -> bool:
        # Quiet is a locked policy set on the config page (the old in-game
        # double-tap toggle was removed in B1: fast tap-stop was misread as a
        # double-tap). force_on = quiet; force_off / legacy in_game = normal.
        policy = str(self._cfg_get("RADIO_QUIET_POLICY", "force_off")).lower()
        return policy == "force_on"

    def quiet_state(self) -> bool:
        return self._quiet_active()

    # ---------------------------------------------------------------- tick

    def tick(self, snapshot: dict, now: float) -> int:
        self._check_new_session(snapshot)
        if not self._enabled():
            self._remember(snapshot)
            return 0
        curr = self._race_model_from(snapshot)
        prev = self._prev_model
        new_events = self._new_events(snapshot)

        # Allow P0 through even in quiet mode (safety). Everything else is off.
        quiet = self._quiet_active()

        names = self.names or self._name_renderer
        if names is None:
            names = names_mod.NameRenderer(str(self._cfg_get("DRIVER_NAME_STYLE", "zh")))
            self._name_renderer = names
        ctx = RuleCtx(prev=prev, curr=curr, snapshot=snapshot,
                      new_events=new_events, now=now,
                      settings=self._cfg, names=names,
                      prev_lap=self._snap_lap)

        emitted = 0
        for rule in self.rules:
            if rule.session_kinds and curr.session_kind not in rule.session_kinds:
                continue
            if not self._verbosity_allows(rule):
                continue
            if quiet and rule.priority != PRIORITY_P0:
                continue
            if not self._cooldown_ok(rule, now):
                continue
            try:
                alert = rule.check(ctx)
            except Exception as e:  # noqa: BLE001 - a bad rule must not stop radio
                self._log.warning("rule %s failed: %r", rule.id, e)
                continue
            if alert is None:
                continue
            if not self._accept(alert, now):
                continue
            self._emit(alert)
            emitted += 1

        self._remember(snapshot)
        return emitted

    # ---------------------------------------------------------------- filters

    def _check_new_session(self, snapshot: dict) -> None:
        """Reset per-session state when the session UID changes.

        state._reset_for_new_session() restarts event seqs at 1 and clears
        laps, but this director kept the previous session's ``_last_event_seq``
        and one-shot dedup keys — so in the SECOND session of a run every
        event-driven rule (safety car, red flag, penalties, overtakes, ...)
        stayed silent for the whole session, and one-shot alerts could never
        refire.
        """
        uid = (snapshot.get("session") or {}).get("session_uid")
        if uid is None or uid == self._session_uid:
            return
        self._session_uid = uid
        self._last_fired.clear()
        self._fired_keys.clear()
        self._last_event_seq = 0
        self._alerts_this_lap = 0
        self._lap_marker = None
        self._prev_model = None
        self._log.info("radio state reset for new session %s", uid)

    def _verbosity_allows(self, rule: Rule) -> bool:
        return _VERBOSITY_RANK[self._verbosity()] >= _VERBOSITY_RANK.get(rule.min_verbosity, 2)

    def _cooldown_ok(self, rule: Rule, now: float) -> bool:
        last = self._last_fired.get(rule.id)
        return last is None or (now - last) >= rule.cooldown_s

    def _per_lap_cap(self) -> int:
        """RADIO_PER_LAP_CAP, capped by the verbosity default.

        The verbosity level stays the ceiling (minimal keeps its tight limit);
        the config value can only tighten it (0 = silence non-safety alerts).
        """
        default = _PER_LAP_CAP.get(self._verbosity(), 6)
        raw = ""
        if self._cfg is not None:
            try:
                raw = str(self._cfg.get("RADIO_PER_LAP_CAP", "") or "")
            except Exception:  # noqa: BLE001
                raw = ""
        if raw.strip():
            try:
                return max(0, min(default, int(float(raw))))
            except (TypeError, ValueError):
                pass
        return default

    def _accept(self, alert: Alert, now: float) -> bool:
        # Dedup: a given dedup_key fires ONCE per session (rules encode the
        # one-shot scope into the key: lap_summary_5, sc_deployed, pos_42...).
        # Previously this only suppressed within the rule cooldown, so a
        # persistent condition (wing damage, final lap) repeated every cooldown
        # for the rest of the race.
        key = alert.dedup_key or alert.id
        if key in self._fired_keys:
            return False
        # Per-lap cap (non-P0 only): the effective cap honours
        # RADIO_PER_LAP_CAP (tightening only).
        lap = (self._snap_lap or {}).get("current_lap_num")
        if lap != self._lap_marker:
            self._lap_marker = lap
            self._alerts_this_lap = 0
        cap = self._per_lap_cap()
        if alert.priority != PRIORITY_P0 and self._alerts_this_lap >= cap:
            return False
        # Global min gap (non-P0).
        min_gap = 8.0
        if alert.priority != PRIORITY_P0:
            try:
                min_gap = float(self._cfg_get("RADIO_MIN_GAP_S", 8.0))
            except Exception:
                min_gap = 8.0
            if (now - self._last_alert_at) < min_gap:
                return False
        self._last_fired[alert.id] = now
        self._fired_keys[key] = now
        # P0 must not eat the per-lap budget or restart the non-safety gap.
        # On minimal (cap 1) one safety call used to silence pit/fuel for the
        # rest of the lap, then hold the next line for RADIO_MIN_GAP_S.
        if alert.priority != PRIORITY_P0:
            self._alerts_this_lap += 1
            self._last_alert_at = now
        return True

    def _emit(self, alert: Alert) -> None:
        self.total_alerts += 1
        if self.sink is not None:
            try:
                self.sink(alert)
            except Exception as e:  # noqa: BLE001
                self._log.warning("alert sink failed: %r", e)

    # ---------------------------------------------------------------- helpers

    def _remember(self, snapshot: dict) -> None:
        self._prev_model = self._race_model_from(snapshot)
        events = snapshot.get("events") or []
        if events:
            self._last_event_seq = max(e.get("seq", 0) for e in events)
        self._snap_lap = (snapshot.get("latest", {}) or {}).get("lap", {}) or {}

    def _new_events(self, snapshot: dict) -> List[dict]:
        events = snapshot.get("events") or []
        return [e for e in events if e.get("seq", 0) > self._last_event_seq]

    def _race_model_from(self, snapshot: dict):
        rm = snapshot.get("race_model")
        if not rm:
            return _EmptyModel()
        return _DictModel(rm)

    def stats(self) -> dict:
        return {"alerts": self.total_alerts, "quiet": self._quiet_active(),
                "verbosity": self._verbosity()}


class _EmptyModel:
    session_kind = "unknown"
    stint = None
    ahead = None
    behind = None
    pit_window = None
    tyre_laps_to_limit = None
    fuel_laps_left = None
    rain_eta_min = None
    field_best_lap_ms = None
    pole_lap_ms = None
    upto_lap = 0
    flags: dict = {}


class _DictModel:
    """Adapt the snapshot's race_model dict to the attribute access rules use."""

    def __init__(self, d: dict) -> None:
        self._d = d or {}

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        v = self._d.get(name)
        return _obj(v) if v is not None else None

    @property
    def session_kind(self):
        return self._d.get("session_kind", "unknown")

    @property
    def flags(self):
        return self._d.get("flags") or {}


def _obj(v):
    if isinstance(v, dict):
        return _DictModel(v)
    return v
