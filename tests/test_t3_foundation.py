"""T3: contracts, config schema, ticker, speech arbiter, voice packs."""

from __future__ import annotations

import time

import pytest

import config_schema
from config import Config
from contracts import Alert, RaceModelState, Stint, Utterance, VoicePack
from ticker import Ticker


# ------------------------------------------------------------------ contracts

def test_contracts_are_constructible():
    a = Alert(id="x", category="p1", priority=1, text="t", created_at=0.0)
    u = Utterance(text="t", priority=0.5, source="rule", created_at=0.0)
    s = Stint(0, 1, "C3", 5, 2.0, 40.0, 0.1, 15.0)
    r = RaceModelState(upto_lap=3, session_kind="race", stint=s)
    v = VoicePack(id="sapi:x", provider="sapi", voice="x")
    assert r.stint is s and v.provider == "sapi"
    assert a.dedup_key == "" and u.gated is True


# ------------------------------------------------------------- config schema

def test_schema_covers_existing_runtime_keys():
    runtime = config_schema.runtime_keys()
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "PROFILE",
                "AUDIO_INPUT", "AUDIO_OUTPUT"):
        assert key in runtime, key
    assert Config.RUNTIME_KEYS == runtime


def test_validate_types_and_bounds():
    assert config_schema.validate("RADIO_ENABLE", "1") is True
    assert config_schema.validate("RADIO_ENABLE", "off") is False
    assert config_schema.validate("RADIO_GAP_EVERY_N", "5") == 5
    assert config_schema.validate("RADIO_GATE_THROTTLE", "0.8") == 0.8
    assert config_schema.validate("RADIO_VERBOSITY", "MINIMAL") == "minimal"
    with pytest.raises(ValueError):
        config_schema.validate("RADIO_VERBOSITY", "loud")
    with pytest.raises(ValueError):
        config_schema.validate("RADIO_GAP_EVERY_N", "999")
    with pytest.raises(ValueError):
        config_schema.validate("NOSUCH", "1")


def test_set_runtime_validates_and_persists_atomically(tmp_path):
    cfg = Config(env_path=tmp_path / ".env")
    cfg.set_runtime("RADIO_VERBOSITY", "normal")
    assert cfg.get("RADIO_VERBOSITY") == "normal"
    assert (tmp_path / ".env").exists()
    # invalid value -> ValueError, nothing written
    with pytest.raises(ValueError):
        cfg.set_runtime("RADIO_VERBOSITY", "bogus")


def test_reload_if_changed_picks_up_file(tmp_path):
    env = tmp_path / ".env"
    cfg = Config(env_path=env)
    assert cfg.reload_if_changed() is False
    time.sleep(0.02)
    env.write_text("RADIO_VERBOSITY=minimal\n", encoding="utf-8")
    # Force mtime difference reliably.
    import os
    os.utime(env, (time.time() + 5, time.time() + 5))
    assert cfg.reload_if_changed() is True
    assert cfg.get("RADIO_VERBOSITY") == "minimal"


def test_is_writable(tmp_path):
    cfg = Config(env_path=tmp_path / ".env")
    assert cfg.is_writable() is True


# ------------------------------------------------------------------- ticker

def test_ticker_run_once_dispatches_and_counts_errors():
    snap = {"a": 1}
    seen = []

    def good(s, now):
        seen.append(("good", s["a"]))

    def bad(s, now):
        raise RuntimeError("boom")

    t = Ticker(snapshot_provider=lambda: snap)
    t.add(good, "good")
    t.add(bad, "bad")
    t.run_once()
    assert seen == [("good", 1)]
    assert t.errors.get("bad") == 1
    assert t.ticks == 1


# ------------------------------------------------------------- speech arbiter

class FakeTTS:
    available = True
    mime = "audio/wav"

    def __init__(self):
        self.texts = []

    def synthesize(self, text):
        self.texts.append(text)
        return text.encode("utf-8")  # distinct audio per text


class FakePlayer:
    def __init__(self):
        self.played = []
        self._playing = False
        self.stopped = 0

    def play(self, audio, mime):
        self.played.append((audio, mime))

    def stop(self):
        self.stopped += 1

    def is_playing(self):
        return False


def _drain(arb, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        with arb._cv:
            if not arb._heap and arb.spoken > 0:
                return
        time.sleep(0.01)


def test_arbiter_priority_order_and_expiry():
    from speech import SpeechArbiter
    tts = FakeTTS()
    player = FakePlayer()
    clock = [0.0]
    arb = SpeechArbiter(tts, player, clock=lambda: clock[0])
    arb.start()
    try:
        # Submit P2 first, then P1; P1 must play first.
        arb.submit(Utterance("p2", 2, "rule", created_at=0.0, gated=False))
        arb.submit(Utterance("p1", 1, "rule", created_at=0.0, gated=False))
        _drain(arb)
        played_texts = [a.decode("utf-8") for a, _ in player.played]
        assert played_texts[0] == "p1", played_texts
    finally:
        arb.stop()


def test_arbiter_interrupt_clears_queue():
    from speech import SpeechArbiter
    arb = SpeechArbiter(FakeTTS(), FakePlayer(), clock=lambda: 0.0)
    arb.start()
    try:
        arb.submit(Utterance("x", 2, "rule", created_at=0.0, gated=False))
        arb.interrupt_all()
        assert arb.stats()["queued"] == 0
        assert arb.player.stopped >= 1
    finally:
        arb.stop()


def test_arbiter_gate_bypassed_for_p0_and_answers():
    from speech import SpeechArbiter
    gate_open = [False]
    played = []

    class P(FakePlayer):
        def play(self, audio, mime):
            played.append(audio)

    arb = SpeechArbiter(FakeTTS(), P(), gate=lambda: gate_open[0],
                        clock=lambda: 0.0)
    arb.start()
    try:
        # gated=False -> plays despite closed gate
        arb.submit(Utterance("p0", 0, "rule", created_at=0.0, gated=False))
        _drain(arb)
        assert len(played) == 1
        # gated=True + closed gate + already expired -> dropped, not played
        arb.submit(Utterance("p2", 2, "rule", created_at=-100.0, gated=True))
        time.sleep(0.3)
        assert len(played) == 1
    finally:
        arb.stop()


def test_arbiter_recording_pause():
    from speech import SpeechArbiter
    played = []

    class P(FakePlayer):
        def play(self, audio, mime):
            played.append(audio)

    arb = SpeechArbiter(FakeTTS(), P(), clock=lambda: 0.0)
    arb.set_recording(True)
    arb.start()
    try:
        arb.submit(Utterance("p0", 0, "rule", created_at=0.0, gated=False))
        time.sleep(0.2)
        assert played == []          # paused while recording
        arb.set_recording(False)
        _drain(arb)
        assert len(played) == 1
    finally:
        arb.stop()


# ----------------------------------------------------------------- voices

def test_voices_list_and_rate_conversion():
    import voices
    from tts_client import _edge_rate, _sapi_rate
    assert _sapi_rate(0) == 0
    assert _sapi_rate("+10%") == 1
    assert _sapi_rate(5) == 5
    assert _sapi_rate(50) == 10        # clamped to SAPI's -10..10
    assert _edge_rate(1) == "+10%"
    assert _edge_rate("+20%") == "+20%"
    packs = voices.list_voices()
    assert all(isinstance(p, VoicePack) for p in packs)
