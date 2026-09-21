"""R6 test: voice link - orchestration, provider selection and HTTP endpoint.

All network-touching parts are stubbed (STT / TTS / engineer), so this runs
fully offline:
  1. stt_client.build_multipart() body construction
  2. VoiceLink.process: happy path, TTS failure -> text-only, STT empty /
     raising / unavailable
  3. make_stt / make_tts provider selection from env (off / unconfigured)
  4. the real webui endpoint: POST /api/ask_voice with a stubbed VoiceLink
     (200 + QA recorded), and 503 when STT is off

Run: py -3.12 tests_voice.py
"""

from __future__ import annotations

import base64
import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import stt_client
import tts_client
from stt_client import STTEngine, build_multipart
from state import TelemetryState
from tts_client import TTSEngine
from voice import VoiceLink


class FakeSTT(STTEngine):
    name = "fake-stt"

    def __init__(self, text="我的圈速多少", raise_exc=None):
        self.text = text
        self.raise_exc = raise_exc
        self.seen = None

    @property
    def available(self):
        return True

    def transcribe(self, audio, mime):
        self.seen = (audio, mime)
        if self.raise_exc:
            raise self.raise_exc
        return self.text


class FakeTTS(TTSEngine):
    name = "fake-tts"
    mime = "audio/mpeg"

    def __init__(self, audio=b"FAKEMP3", raise_exc=None):
        self.audio = audio
        self.raise_exc = raise_exc

    @property
    def available(self):
        return True

    def synthesize(self, text):
        if self.raise_exc:
            raise self.raise_exc
        return self.audio


class FakeEngineer:
    def __init__(self):
        self.client = type("C", (), {"last_usage": None})()
        self.questions = []

    def ask(self, question, snapshot):
        self.questions.append(question)
        return "上一圈 1:23.055，最快圈 1:21.000。"


class StubRecorder:
    def __init__(self):
        self.qa = []

    def record_qa(self, q, a, usage):
        self.qa.append((q, a, usage))


def check_multipart():
    body, ctype = build_multipart(
        {"model": "whisper-1", "language": "zh"},
        b"\x01\x02AUDIO", "audio.webm", "audio/webm")
    assert ctype.startswith("multipart/form-data; boundary="), ctype
    assert b'name="model"' in body and b"whisper-1" in body
    assert b'name="language"' in body and b"zh" in body
    assert b'filename="audio.webm"' in body
    assert b"Content-Type: audio/webm\r\n" in body
    assert b"\x01\x02AUDIO" in body
    assert body.endswith(b"--\r\n"), "closing boundary missing"
    print("[ok] multipart builder")


def check_voice_link():
    eng = FakeEngineer()
    st = TelemetryState()

    # happy path
    vl = VoiceLink(eng, st, stt=FakeSTT(), tts=FakeTTS())
    assert vl.available and vl.tts_available
    res = vl.process(b"AUDIO", "audio/webm;codecs=opus")
    print("[ok] happy path:", res["question"], "->", res["answer"])
    assert res["question"] == "我的圈速多少"
    assert res["answer"].startswith("上一圈")
    assert res["audio_b64"] == base64.b64encode(b"FAKEMP3").decode()
    assert res["audio_mime"] == "audio/mpeg"
    assert vl.stt.seen == (b"AUDIO", "audio/webm;codecs=opus") or \
        vl.stt.seen[0] == b"AUDIO"
    assert eng.questions == ["我的圈速多少"]

    # TTS failure -> text-only, never fails the answer
    vl2 = VoiceLink(FakeEngineer(), st, stt=FakeSTT(),
                    tts=FakeTTS(raise_exc=RuntimeError("edge down")))
    res2 = vl2.process(b"AUDIO", "audio/webm")
    assert res2["answer"].startswith("上一圈") and res2["audio_b64"] is None, res2
    print("[ok] TTS failure degrades to text-only")

    # STT empty / raising / unavailable
    res3 = VoiceLink(FakeEngineer(), st, stt=FakeSTT(text="   ")).process(b"A", "audio/webm")
    assert res3 == {"error": "empty transcription"}, res3
    res4 = VoiceLink(FakeEngineer(), st, stt=FakeSTT(raise_exc=RuntimeError("net"))).process(b"A", "audio/webm")
    assert res4["error"].startswith("STT failed"), res4
    res5 = VoiceLink(FakeEngineer(), st, stt=None, tts=FakeTTS()).process(b"A", "audio/webm")
    assert res5["error"].startswith("voice unavailable"), res5
    assert not VoiceLink(FakeEngineer(), st, stt=None, tts=None).available
    print("[ok] STT empty / raising / unavailable paths")


def check_provider_selection():
    import os
    old = {k: os.environ.get(k) for k in ("STT_PROVIDER", "TTS_PROVIDER")}
    try:
        os.environ["STT_PROVIDER"] = "off"
        os.environ["TTS_PROVIDER"] = "off"
        assert stt_client.make_stt() is None
        assert tts_client.make_tts() is None
        # unconfigured auto-detect in this sandbox: no key, no libs, not Windows
        del os.environ["STT_PROVIDER"]
        eng = stt_client.make_stt()
        assert eng is None or isinstance(eng, STTEngine)
        assert not CloudSTTUnconfigured()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    print("[ok] provider selection (off / auto)")


def CloudSTTUnconfigured():
    c = stt_client.CloudSTT()
    return c.available  # False unless somebody configured a key in .env


def check_http_endpoint():
    from webui import _Handler
    eng = FakeEngineer()
    st = TelemetryState()
    rec = StubRecorder()
    vl = VoiceLink(eng, st, stt=FakeSTT(), tts=FakeTTS())

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.ctx = {"voice": vl, "engineer": eng, "state": st, "recorder": rec}
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/ask_voice", data=b"AUDIOBYTES",
            headers={"Content-Type": "audio/webm;codecs=opus"}, method="POST")
        resp = json.loads(urllib.request.urlopen(req, timeout=5).read().decode())
        print("[ok] endpoint 200:", resp["question"], "->", resp["answer"])
        assert resp["question"] == "我的圈速多少"
        assert resp["audio_b64"] == base64.b64encode(b"FAKEMP3").decode()
        assert rec.qa and rec.qa[0][0] == "我的圈速多少", "QA not recorded"

        # oversized body -> 413
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port}/api/ask_voice",
                data=b"x" * (5 * 1024 * 1024 + 1),
                headers={"Content-Type": "audio/webm"}, method="POST"), timeout=10)
            raise AssertionError("oversized upload was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 413, e.code
        print("[ok] endpoint 413 for oversized audio")
    finally:
        httpd.shutdown()
        httpd.server_close()

    # STT off -> 503
    off = VoiceLink(FakeEngineer(), TelemetryState(), stt=None, tts=None)
    httpd2 = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd2.ctx = {"voice": off, "recorder": StubRecorder()}
    port2 = httpd2.server_address[1]
    t2 = threading.Thread(target=httpd2.serve_forever, daemon=True)
    t2.start()
    try:
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"http://127.0.0.1:{port2}/api/ask_voice", data=b"AUDIO",
                headers={"Content-Type": "audio/webm"}, method="POST"), timeout=5)
            raise AssertionError("voice-off should 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503, e.code
        print("[ok] endpoint 503 when STT off")
    finally:
        httpd2.shutdown()
        httpd2.server_close()


def main():
    check_multipart()
    check_voice_link()
    check_provider_selection()
    check_http_endpoint()
    print("\nVOICE LINK OK")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
