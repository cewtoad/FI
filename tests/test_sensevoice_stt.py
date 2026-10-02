"""LocalSenseVoiceSTT tests — hermetic via a fake sherpa_onnx module."""

from __future__ import annotations

import io
import sys
import types
import wave

import pytest

import stt_client


class _FakeResult:
    def __init__(self, text):
        self.text = text


class _FakeStream:
    def accept_waveform(self, rate, samples):
        _FakeStream.last_rate = rate
        _FakeStream.last_len = len(samples)

    result = None


class _FakeRecognizer:
    instances = 0

    def __init__(self, **kw):
        self.kw = kw
        _FakeRecognizer.instances += 1

    @classmethod
    def from_sense_voice(cls, **kw):
        return cls(**kw)

    def create_stream(self):
        return _FakeStream()

    def decode_streams(self, streams):
        for s in streams:
            s.result = _FakeResult("<|zh|><|NEUTRAL|>你好 世界")


def _fake_lib(monkeypatch):
    mod = types.ModuleType("sherpa_onnx")
    mod.OfflineRecognizer = _FakeRecognizer
    monkeypatch.setattr(stt_client.LocalSenseVoiceSTT, "_lib", lambda self: mod)
    return mod


def _model_dir(tmp_path):
    d = tmp_path / "sensevoice"
    d.mkdir(parents=True, exist_ok=True)
    (d / "model.int8.onnx").write_bytes(b"x" * 1024)
    (d / "tokens.txt").write_text("a b c\n", encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeRecognizer.instances = 0
    _FakeStream.last_rate = None
    _FakeStream.last_len = None
    yield


def test_available_requires_lib_and_model(monkeypatch, tmp_path):
    _fake_lib(monkeypatch)
    sv = stt_client.LocalSenseVoiceSTT(model_dir=str(tmp_path / "empty"))
    assert sv.available is False            # lib present, model missing
    sv2 = stt_client.LocalSenseVoiceSTT(model_dir=str(_model_dir(tmp_path)))
    assert sv2.available is True


def test_transcribe_strips_tags_and_loads_once(monkeypatch, tmp_path):
    _fake_lib(monkeypatch)
    sv = stt_client.LocalSenseVoiceSTT(model_dir=str(_model_dir(tmp_path)))
    assert sv.transcribe([0.0, 0.1, -0.1]) == "你好 世界"
    assert _FakeStream.last_rate == 16000
    assert _FakeRecognizer.instances == 1
    sv.transcribe([0.0])                    # second call: no second load
    assert _FakeRecognizer.instances == 1


def test_transcribe_wav_bytes(monkeypatch, tmp_path):
    _fake_lib(monkeypatch)
    sv = stt_client.LocalSenseVoiceSTT(model_dir=str(_model_dir(tmp_path)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x40" * 320)    # 320 samples of small sine-ish
    assert sv.transcribe(buf.getvalue()) == "你好 世界"
    assert _FakeStream.last_rate == 16000
    assert _FakeStream.last_len == 320


def test_make_stt_engine_switch_and_fallback(monkeypatch, tmp_path):
    _fake_lib(monkeypatch)
    model_dir = _model_dir(tmp_path)

    class Cfg:
        def __init__(self, engine):
            self.engine = engine

        def get(self, k, d=None):
            if k == "STT_PROVIDER":
                return "local"
            if k == "STT_LOCAL_ENGINE":
                return self.engine
            return d

        def get_int(self, k, d=2):
            return d

    class _StubWhisper:
        available = True

        def __init__(self, **kw):
            self.kw = kw

    monkeypatch.setattr(stt_client, "LocalWhisperSTT", _StubWhisper)
    # sensevoice configured and available -> SenseVoice engine.
    monkeypatch.setattr(stt_client, "get_config", lambda: Cfg("sensevoice"))
    eng = stt_client.make_stt(model_dir=str(model_dir))
    assert isinstance(eng, stt_client.LocalSenseVoiceSTT)
    # sensevoice configured but model missing -> falls back to whisper.
    monkeypatch.setattr(stt_client, "get_config", lambda: Cfg("sensevoice"))
    eng = stt_client.make_stt(model_dir=str(tmp_path / "missing"))
    assert isinstance(eng, _StubWhisper)
    # whisper explicitly configured -> whisper even when sensevoice available.
    monkeypatch.setattr(stt_client, "get_config", lambda: Cfg("whisper"))
    eng = stt_client.make_stt(model_dir=str(model_dir))
    assert isinstance(eng, _StubWhisper)


def test_engine_schema():
    import config_schema
    s = config_schema.SCHEMA_BY_KEY["STT_LOCAL_ENGINE"]
    assert s.choices == ("sensevoice", "whisper")
    assert config_schema.SCHEMA_BY_KEY["STT_LOCAL_MODEL"].default == "small"
