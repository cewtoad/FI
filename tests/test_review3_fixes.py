"""Regression tests for the third review round (2026-10-02 fixes).

Covers:
  - fast-answer routing: "前面还剩多少圈" must hit laps_remaining, not the
    car-ahead route; rival pace needs an explicit pace word (no bare "多少")
  - cloud STT: single-arg call with raw float PCM (voice_main path) works
  - webui: Origin is checked against the request Host (LAN binding works);
    /api/profile POST is local-only
  - race model: pit-window latch is re-armed on a session change
  - state: a CAR_STATUS rebuild between FLBK and the rewound LAP_DATA must
    not poison the position baseline (fake "dropped places" event)
"""

from __future__ import annotations

import json
import logging
import struct
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from fake_data import make_lap, make_status
from lib.f1_types import F1PacketType, PacketHeader
from lib.telemetry_manager import PacketParserFactory
from profiles import LocalRouter
from race_model import RaceModel
from receiver import PACKETS_CONSUMED
from state import TelemetryState


# ------------------------------------------------------------- local router

def test_laps_question_is_not_hijacked_by_car_ahead_route():
    facts = {"car_ahead": "诺里斯", "laps_remaining": 8}
    out = LocalRouter().answer("前面还剩多少圈", facts)
    assert out is not None and out.intent == "laps_remaining", out
    assert "8 圈" in out.text


def test_car_ahead_route_still_answers_who_is_ahead():
    out = LocalRouter().answer("前面是谁", {"car_ahead": "诺里斯"})
    assert out is not None and out.intent == "car_ahead", out


class _Names:
    _drivers = [{"code": "HAM", "zh": "汉密尔顿"}]


def test_rival_pace_needs_a_pace_word_not_bare_duoshao():
    lb = [{"driver": "L. Hamilton", "last_lap_ms": 82000}]
    router = LocalRouter()
    # "多少" alone used to pull any driver-name question into rival-pace.
    out = router.answer("汉密尔顿还剩多少圈", {"laps_remaining": 8},
                        leaderboard=lb, name_renderer=_Names())
    assert out is not None and out.intent == "laps_remaining", out
    # An explicit pace word still routes to the rival.
    out2 = router.answer("汉密尔顿圈速多少", {}, leaderboard=lb,
                         name_renderer=_Names())
    assert out2 is not None and out2.intent == "rival_pace", out2


# ---------------------------------------------------------------- cloud STT

def test_pcm_to_wav_roundtrip_clamps_hot_signal():
    from stt_client import _wav_bytes_to_f32, pcm_to_wav_bytes
    wav = pcm_to_wav_bytes([0.0, 0.5, -0.5, 1.5, -1.5])
    assert wav[:4] == b"RIFF"
    rate, samples = _wav_bytes_to_f32(wav)
    assert rate == 16000 and len(samples) == 5
    assert abs(samples[1] - 0.5) < 0.001
    assert samples[3] > 0.99 and samples[4] < -0.99


def test_cloud_stt_transcribe_accepts_raw_pcm_single_arg(monkeypatch):
    import stt_client
    stt = stt_client.CloudSTT.__new__(stt_client.CloudSTT)
    stt.api_key = "k"
    stt.base_url = "http://127.0.0.1:9"
    stt.model = "whisper-1"
    stt.language = "zh"
    stt.timeout = 1.0
    seen = {}

    class FakeResp:
        def read(self):
            return '{"text": "测试"}'.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen["body"] = req.data
        return FakeResp()

    monkeypatch.setattr(stt_client.urllib.request, "urlopen", fake_urlopen)
    # voice_main calls transcribe(pcm) with one argument - must not TypeError.
    out = stt.transcribe([0.0, 0.5, -0.5])
    assert out == "测试"
    assert b"RIFF" in seen["body"], "raw PCM was not wrapped as WAV"
    assert b'filename="audio.wav"' in seen["body"]


# ------------------------------------------------------------------- webui

class _Headers:
    def __init__(self, values):
        self._v = values

    def get(self, key, default=None):
        for k, v in self._v.items():
            if k.lower() == key.lower():
                return v
        return default


def test_origin_guard_matches_request_host_for_lan_access():
    import webui
    h = object.__new__(webui._Handler)
    h.headers = _Headers({"Origin": "http://192.168.1.50:8766",
                          "Host": "192.168.1.50:8766"})
    assert h._origin_ok() is True                      # LAN same-origin POST
    h.headers = _Headers({"Origin": "http://evil.example.com",
                          "Host": "192.168.1.50:8766"})
    assert h._origin_ok() is False                     # cross-site stays blocked
    h.headers = _Headers({"Origin": "null", "Host": "127.0.0.1:8766"})
    assert h._origin_ok() is False                     # sandboxed iframe
    h.headers = _Headers({"Host": "127.0.0.1:8766"})
    assert h._origin_ok() is True                      # non-browser client


def test_profile_post_is_local_only(monkeypatch):
    import webui
    monkeypatch.setattr(webui._Handler, "_local_only", lambda self: False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    httpd.ctx = {"state": None, "summariser": None, "receiver": None,
                 "engineer": None, "voice": None, "recorder": None}
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/profile",
            data=json.dumps({"profile": "fast"}).encode(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 403, code
    finally:
        httpd.shutdown()
        httpd.server_close()


# -------------------------------------------------------------- race model

def _rm_snap(uid, cur, ideal, latest, stops, pit="NONE"):
    return {
        "session": {"session_uid": uid, "session_kind": "race", "total_laps": 50,
                    "pit_window_ideal_lap": ideal,
                    "pit_window_latest_lap": latest,
                    "pit_window_rejoin_position": 4,
                    "safety_car_status": "NO SAFETY CAR",
                    "weather_forecast": [], "marshal_yellow_zones": []},
        "latest": {"lap": {"current_lap_num": cur, "lap_distance_m": 0.0,
                           "pit_status": pit, "num_pit_stops": stops},
                   "status": {}},
        "fuel": {"surplus_laps": 1.0},
        "lap_snapshots": [{"lap_num": 1, "lap_time_ms": 85000, "valid": True,
                           "tyre_compound": "C3", "tyre_age_laps": 1,
                           "tyre_wear_max_pct": 2.0,
                           "safety_car": "NO SAFETY CAR",
                           "pit_this_lap": False}],
        "leaderboard": [],
    }


def test_pit_window_latch_resets_on_new_session():
    m = RaceModel()
    # Session 1: window armed at stop count 0, then a stop lands -> done.
    m.update(_rm_snap(111, cur=9, ideal=10, latest=12, stops=0), 0.0)
    assert m.latest.pit_window.state == "not_open"
    m.update(_rm_snap(111, cur=11, ideal=10, latest=12, stops=1), 1.0)
    assert m.latest.pit_window.state == "done"
    # Session 2, same window numbers, player already carrying one stop. The
    # stale latch must not mark the fresh window done before it opens.
    m.update(_rm_snap(222, cur=9, ideal=10, latest=12, stops=1), 2.0)
    assert m.latest.pit_window.state == "not_open"
    assert m._pit_window_stop0 == 1


# ------------------------------------------------------------------- state

FMT = 2025
UID = 55


def _hdr(pid, frame, t):
    return PacketHeader.from_values(
        packet_format=FMT, game_year=25, game_major_version=1,
        game_minor_version=0, packet_version=1, packet_type=pid,
        session_uid=UID, session_time=t, frame_identifier=frame,
        overall_frame_identifier=frame, player_car_index=0,
        secondary_player_car_index=255)


def _flbk(frame, t):
    return (_hdr(F1PacketType.EVENT, frame, t).to_bytes()
            + b"FLBK" + struct.pack("<If", 100, 12.5))


def _lap(frame, t, pos, lap_num):
    return make_lap(FMT, F1PacketType.LAP_DATA, UID, frame, t,
                    last_lap_ms=0, cur_lap_ms=30000, lap_distance=1000.0,
                    total_distance=1000.0, cur_lap_num=lap_num,
                    position=pos, sector=1)


def test_flashback_position_baseline_is_not_repolluted_by_car_status():
    st = TelemetryState()
    f = PacketParserFactory(PACKETS_CONSUMED, logging.getLogger("test_review3"))

    def feed(raw):
        pkt = f.parse(raw)
        assert pkt is not None, f.last_failure_reason
        st.process(pkt)

    feed(_lap(1, 0.0, pos=5, lap_num=5))
    assert st._last_position == 5
    feed(_flbk(2, 2.0))                    # baseline cleared, rollback pending
    feed(make_status(FMT, UID, 3, 3.0, 50.0, 3.0, 5))   # stale CAR_STATUS
    assert st._last_position is None, "CAR_STATUS re-latched the pre-rewind position"
    feed(_lap(4, 4.0, pos=2, lap_num=3))   # rewound LAP_DATA -> rollback runs
    feed(make_status(FMT, UID, 5, 5.0, 50.0, 3.0, 5))
    assert st._last_position == 2
    kinds = [e["kind"] for e in st.snapshot()["events"]]
    assert "position_down" not in kinds and "position_up" not in kinds, kinds
    # A real position change afterwards is still detected.
    feed(_lap(6, 6.0, pos=1, lap_num=4))
    kinds2 = [e["kind"] for e in st.snapshot()["events"]]
    assert "position_up" in kinds2, kinds2
