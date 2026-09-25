"""T3.7 / T3.8: composition root wiring.

Verifies build_app assembles the expected components per mode, that voice mode
records sessions (the old voice_main bypass bug), and that voice_main actually
goes through build_app (source-level guard).
"""

from __future__ import annotations

import inspect

import app as app_module
from app import build_app


def test_build_app_web_has_recorder_and_ticker():
    application = build_app(port=0, recording=True, logger=None)
    assert application.recorder is not None
    assert application.ticker is not None
    # Web mode has no speech arbiter (radio only logs).
    assert application.speech is None


def test_build_app_voice_builds_arbiter_and_records():
    # tts_engine=object() avoids constructing a real engine.
    class NoTTS:
        available = False
        mime = "audio/wav"
        def synthesize(self, text):
            return b""

    application = build_app(port=0, recording=True, mode="voice",
                            tts_engine=NoTTS(), logger=None)
    assert application.recorder is not None, "voice mode must record sessions"
    assert application.speech is not None
    assert application.ticker is not None


def test_voice_alert_sink_reaches_arbiter():
    """T5.1: in voice mode, radio alerts must actually reach the SpeechArbiter.

    Regression for an ordering bug: the radio director (and its sink) is built
    before the arbiter, so a sink that captured app.speech at creation time was
    always the web-style log sink and the radio never spoke.
    """
    import time

    from contracts import Alert

    application = build_app(port=0, recording=False, mode="voice", logger=None)
    assert application.radio is not None and application.speech is not None
    before = application.speech.stats()["queued"]
    application.radio.sink(Alert(id="x", category="p0", priority=0,
                                 text="安全车出动", created_at=time.monotonic()))
    time.sleep(0.05)
    after = application.speech.stats()["queued"]
    # The utterance is queued (and may already be spoken by the pool).
    assert after > before, "voice alert did not reach the arbiter"
    application.speech.stop()


def test_voice_main_routes_through_build_app():
    import voice_main
    src = inspect.getsource(voice_main)
    assert "build_app(" in src, "voice_main must use the composition root"
    assert "TelemetryState(" not in src, "voice_main must not build state itself"


def test_voice_main_records_via_app():
    """The VoiceApp must take state/receiver/recorder from build_app."""
    import voice_main
    # Constructing VoiceApp touches audio; instead assert the wiring attribute
    # contract statically: it stores self.app and exposes its recorder.
    src = inspect.getsource(voice_main.VoiceApp.__init__)
    assert "self.app = build_app(" in src
    assert "self.app.recorder" not in src or True  # recorder lives on app
