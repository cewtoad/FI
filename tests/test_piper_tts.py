"""T3.5 / stop point #4: Piper TTS provider (fully local, WAV output).

Uses a fake `piper` module so the test runs without piper-tts installed.
"""

from __future__ import annotations

import sys
import types
import wave

import pytest

from contracts import VoicePack
from tts_client import PiperTTS, _piper_length_scale


class _FakeVoice:
    def __init__(self, path):
        self.path = path

    def synthesize_wav(self, text, wav_file):
        # Write a tiny valid mono WAV.
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(22050)
        wav_file.writeframes(b"\x00\x00" * 100)


class _FakePiper:
    def __init__(self):
        self.loaded = []

        class PiperVoice:
            @staticmethod
            def load(path):
                return _FakeVoice(path)

        self.PiperVoice = PiperVoice


@pytest.fixture()
def fake_piper(monkeypatch, tmp_path):
    mod = _FakePiper()
    monkeypatch.setitem(sys.modules, "piper", mod)
    model = tmp_path / "zh_CN-huayan-medium.onnx"
    model.write_bytes(b"fake-onnx")
    return mod, str(model)


def test_piper_available_only_with_model(fake_piper, monkeypatch):
    _mod, model = fake_piper
    eng = PiperTTS(voice=model)
    assert eng.available is True
    assert eng.mime == "audio/wav"
    # An unconfigured voice (env returns "") must be unavailable. Isolate from
    # the developer's real .env via a stub config.
    import config as config_mod
    import tts_client

    class _Cfg:
        def get(self, key, default=""):
            return default

    monkeypatch.setattr(tts_client, "get_config", lambda: _Cfg())
    eng2 = PiperTTS(voice="")
    assert eng2.available is False


def test_piper_synthesize_returns_wav(fake_piper):
    _mod, model = fake_piper
    eng = PiperTTS(voice=model)
    data = eng.synthesize("你好，世界")
    with wave.open(__import__("io").BytesIO(data), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getframerate() == 22050


def test_piper_missing_model_raises(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "piper", _FakePiper())
    eng = PiperTTS(voice=str(tmp_path / "nope.onnx"))
    with pytest.raises(RuntimeError):
        eng.synthesize("x")


def test_piper_length_scale_conversion():
    assert _piper_length_scale(0) == 1.0
    assert _piper_length_scale("+10%") == pytest.approx(0.5)   # faster
    assert _piper_length_scale(-10) == pytest.approx(1.5)      # slower
    assert _piper_length_scale(None) is None


def test_voices_provider_returns_piper(fake_piper):
    import voices
    _mod, model = fake_piper
    pack = VoicePack(id="piper:x", provider="piper", voice=model)
    eng = voices.make_tts(pack)
    assert eng is not None and eng.name == "piper"
