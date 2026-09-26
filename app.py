"""Composition root: build the wired application once, for every entry point.

run.py / webui.py / voice_main.py previously assembled receiver + state +
engineer + recorder + voice themselves, each slightly differently. This module
is the single place that knows how they fit together; entries become thin.

    build_app(port, bind_ip, recording=True) -> App

The receiver gets an ``on_packet`` hook that persists laps as soon as the
session goes live, so recording no longer depends on a browser polling
``/api/state``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from engineer import Engineer
from lib.f1_types import F1PacketType
from receiver import DEFAULT_PORT, PACKETS_CONSUMED, TelemetryReceiver
from recorder import SessionRecorder
from state import TelemetryState
from summariser import Summariser

import paths


@dataclass
class App:
    state: TelemetryState
    receiver: TelemetryReceiver
    summariser: Summariser
    engineer: Engineer
    recorder: Optional[SessionRecorder] = None
    extras: dict = field(default_factory=dict)
    mode: str = "web"
    ticker: Any = None
    race_model: Any = None
    radio: Any = None
    speech: Any = None

    def ctx(self) -> dict:
        """The context dict the HTTP layer expects."""
        return {
            "state": self.state,
            "receiver": self.receiver,
            "summariser": self.summariser,
            "engineer": self.engineer,
            "recorder": self.recorder,
            **self.extras,
        }

    def start_background(self) -> None:
        """Start the ticker (and speech arbiter) if present."""
        if self.speech is not None:
            try:
                self.speech.start()
            except Exception as e:  # noqa: BLE001
                # Previously swallowed silently: voice mode would run with no
                # speech output and zero clues. Log it.
                logging.getLogger("f1_tr").warning(
                    "speech arbiter failed to start: %r", e)
        if self.ticker is not None:
            self.ticker.start()

    def shutdown(self) -> None:
        if self.ticker is not None:
            try:
                self.ticker.stop()
            except Exception:
                pass
        if self.speech is not None:
            try:
                self.speech.stop()
            except Exception:
                pass
        raw_fh = self.extras.get("raw_fh")
        if raw_fh is not None:
            try:
                raw_fh.close()
            except Exception:
                pass


def build_app(port: int = DEFAULT_PORT, bind_ip: str = "127.0.0.1",
              logger: Optional[logging.Logger] = None,
              recording: bool = True,
              record_raw: bool = False,
              raw_path: Optional[str] = None,
              mode: str = "web",
              tts_engine: Any = None,
              audio_output: str = "") -> App:
    logger = logger or logging.getLogger("f1_tr")
    state = TelemetryState(error_logger=logger)
    recorder = SessionRecorder() if recording else None

    # T1.6: optional raw UDP recording. Off by default so a normal run does
    # not fill the disk; enable via --raw / F1TR_RECORD_RAW for replay capture.
    raw_sink = None
    raw_fh = None
    if record_raw:
        import time as _time
        from pathlib import Path as _Path

        from tools.udp_record import write_record
        raw_dir = _Path(raw_path) if raw_path else (paths.app_root() / "sessions")
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_file = raw_dir / f"raw_{_time.strftime('%Y%m%d_%H%M%S')}.f1rec"
        raw_fh = open(raw_file, "wb")
        t0 = _time.monotonic()

        def raw_sink(payload: bytes) -> None:  # type: ignore[misc]
            write_record(raw_fh, int((_time.monotonic() - t0) * 1000), payload)
        logger.info("recording raw UDP packets to %s", raw_file)

    # Keep the receive hot path cheap: persist at most RECORD_HZ times a
    # second. (The old "plus every LAP_DATA" exception fired on every LAP_DATA
    # packet - up to 60Hz - forcing a full snapshot rebuild each time; laps
    # come from SESSION_HISTORY anyway, which the 2Hz beat picks up.)
    record_hz = 2.0
    last_record = [0.0]
    record_errors = [0]

    def _on_packet(packet: Any) -> None:
        if recorder is None:
            return
        now = time.monotonic()
        if (now - last_record[0]) < (1.0 / record_hz):
            return
        last_record[0] = now
        try:
            recorder.record_state(state.snapshot())
        except Exception as e:  # noqa: BLE001 - never break the receive path
            # Counted and surfaced at a low rate: a persistently failing
            # recorder must be diagnosable without spamming the RX log.
            record_errors[0] += 1
            if record_errors[0] <= 3 or record_errors[0] % 50 == 0:
                logger.warning("recorder.record_state failed (%d): %r",
                               record_errors[0], e)

    receiver = TelemetryReceiver(
        state, port=port, bind_ip=bind_ip,
        interested=PACKETS_CONSUMED, logger=logger,
        on_packet=_on_packet, raw_sink=raw_sink)
    engineer = Engineer()
    app = App(state=state, receiver=receiver, summariser=Summariser(),
              engineer=engineer, recorder=recorder, mode=mode)
    if raw_fh is not None:
        app.extras["raw_file"] = str(raw_file)
        app.extras["raw_fh"] = raw_fh

    # ---- T3.8: race model + ticker + (voice) speech arbiter ----
    _assemble_pipeline(app, logger=logger, tts_engine=tts_engine,
                       audio_output=audio_output)
    return app


def _assemble_pipeline(app: "App", logger, tts_engine=None,
                       audio_output: str = "") -> None:
    """Wire the race model, radio director, ticker and (voice) arbiter.

    Uses lazy imports and tolerates the pieces not existing yet so the
    composition root stays the single wiring point across tasks.
    """
    from ticker import Ticker

    # Race model (T4). Optional so build_app works before/without it.
    race_model = None
    try:
        from race_model import RaceModel
        race_model = RaceModel(config=get_config())
        # Merge {"race_model": <RaceModelState dict>} into the snapshot.
        state_dict = getattr(race_model, "latest_dict", None)
        if state_dict is not None:
            app.state.add_snapshot_provider(lambda: {"race_model": state_dict()})
        app.race_model = race_model
    except Exception as e:  # noqa: BLE001
        logger.debug("race model unavailable: %r", e)

    # Radio director (T5). Optional. Its AlertSink differs by mode.
    try:
        from radio_director import RadioDirector
        from radio_rules import build_default_rules

        sink = _make_alert_sink(app, logger)
        radio = RadioDirector(rules=build_default_rules(),
                              alert_sink=sink,
                              config=get_config())
        app.radio = radio
    except Exception as e:  # noqa: BLE001
        logger.debug("radio director unavailable: %r", e)

    # Speech arbiter: only for voice mode (web mode uses the AlertLog only).
    if app.mode == "voice":
        try:
            from speech import AudioPlayer, SpeechArbiter
            from voices import make_tts as make_voice_tts

            engine = tts_engine or make_voice_tts()
            player = AudioPlayer(output_device=audio_output, logger=logger)
            gate = None
            if app.state is not None:
                def gate():  # noqa: E306
                    hold = get_config().get_float("RADIO_GATE_HOLD_S", 0.5)
                    return app.state.is_on_straight(hold)
            app.speech = SpeechArbiter(engine, player, gate=gate,
                                       clock=time.monotonic, logger=logger)
        except Exception as e:  # noqa: BLE001
            logger.debug("speech arbiter unavailable: %r", e)

    # Ticker: drives race model + radio director every beat. Config hot-reload.
    ticker = Ticker(rate_hz=2.0,
                    snapshot_provider=app.state.snapshot,
                    logger=logger)

    # One handler per stage so the ticker's per-handler isolation/counters
    # apply (a radio failure must not skip the debrief, and vice versa).
    def _reload(snapshot: dict, now: float) -> None:
        if get_config().reload_if_changed() and app.engineer is not None:
            # The config page is a separate process: after it rewrites .env,
            # rebuild the cached LLM client so fallback/timeout edits hot-apply
            # instead of staying pinned at the old construction-time values.
            app.engineer.refresh_client()

    def _radio(snapshot: dict, now: float) -> None:
        if app.radio is None:
            return
        if race_model is not None:
            # The snapshot's race_model was merged before this beat's update;
            # hand the radio the fresh one (shallow copy: the frozen snapshot
            # is shared with other reader threads and must not be mutated).
            snapshot = {**snapshot, "race_model": race_model.latest_dict()}
        app.radio.tick(snapshot, now)

    def _debrief(snapshot: dict, now: float) -> None:
        writer = app.extras.get("debrief")
        if writer is not None:
            writer(snapshot, now)

    ticker.add(_reload, "config_reload")
    if race_model is not None:
        ticker.add(race_model.update, "race_model")
    ticker.add(_radio, "radio")
    ticker.add(_debrief, "debrief")
    # T8: end-of-session debrief writer (local TXT, no LLM).
    try:
        from debrief import DebriefWriter
        app.extras["debrief"] = DebriefWriter(config=get_config(), logger=logger)
    except Exception as e:  # noqa: BLE001
        logger.debug("debrief writer unavailable: %r", e)
    app.ticker = ticker


def _make_alert_sink(app: "App", logger):
    """Return a callable(Alert) -> None for the radio director (T5.1).

    In voice mode the sink forwards alerts to the SpeechArbiter so they are
    spoken; the arbiter is created *after* the radio director, so it is
    resolved lazily at call time (not captured here).
    """
    from contracts import PRIORITY_ANSWER
    mode = app.mode

    if mode == "voice":
        def _voice_sink(alert):
            speech = app.speech
            if speech is None:
                return
            from contracts import Utterance
            speech.submit(Utterance(
                text=alert.text, priority=alert.priority, source="rule",
                created_at=alert.created_at, dedup_key=alert.dedup_key,
                gated=(alert.priority > PRIORITY_ANSWER)))
        return _voice_sink

    # Web / console: keep an in-memory alert log (exposed via /api/state).
    from collections import deque
    log = deque(maxlen=200)
    app.extras["alert_log"] = log

    def _log_sink(alert):
        log.append({
            "id": alert.id, "category": alert.category,
            "priority": alert.priority, "text": alert.text,
            "created_at": alert.created_at,
        })
    return _log_sink


def get_config():
    from config import get_config as _gc
    return _gc()
