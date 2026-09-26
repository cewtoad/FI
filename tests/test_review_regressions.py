"""Regression tests for the Opus review findings (S4/S5/S6/S7/M3/M12/S3)."""

from __future__ import annotations

import json
import socket
import time

from contracts import PRIORITY_P1, PRIORITY_P2, Utterance


class _TTS:
    available = True
    mime = "audio/wav"

    def synthesize(self, text):
        if text == "BAD":
            raise RuntimeError("SAPI hiccup")
        return text.encode()


class _Player:
    """Each clip 'plays' for 0.2s so overlap is observable."""

    def __init__(self):
        self.started = []
        self._until = 0.0

    def play(self, audio, mime):
        now = time.monotonic()
        self.started.append((audio, now < self._until))
        self._until = now + 0.2

    def stop(self):
        self._until = 0.0

    def is_playing(self):
        return time.monotonic() < self._until


def _run(utts, wait=1.2):
    from speech import SpeechArbiter
    p = _Player()
    a = SpeechArbiter(_TTS(), p)
    a.start()
    try:
        now = time.monotonic()
        for text, prio in utts:
            a.submit(Utterance(text=text, priority=prio, source="rule",
                               created_at=now, gated=False))
        time.sleep(wait)
    finally:
        a.stop()
    return p, a


def test_failed_synthesis_does_not_block_queue():            # S4
    p, a = _run([("BAD", PRIORITY_P1), ("ok", PRIORITY_P2)])
    assert [x[0] for x in p.started] == [b"ok"]
    assert a.dropped_failed == 1


def test_empty_text_is_not_queued():                         # S4
    p, a = _run([("", PRIORITY_P1), ("ok", PRIORITY_P2)])
    assert [x[0] for x in p.started] == [b"ok"]


def test_clips_do_not_cut_each_other_off():                  # S5
    p, _ = _run([("c1", PRIORITY_P2), ("c2", PRIORITY_P2), ("c3", PRIORITY_P2)])
    assert [x[0] for x in p.started] == [b"c1", b"c2", b"c3"]
    assert not any(overlap for _, overlap in p.started)


def test_sapi_never_splices_text_into_script(monkeypatch):   # S6
    import subprocess

    import tts_client
    seen = {}

    def fake_run(args, **kw):
        seen["script"] = args[-1]
        seen["env"] = kw.get("env") or {}
        raise RuntimeError("stop here")

    monkeypatch.setattr(subprocess, "run", fake_run)
    eng = tts_client.SapiTTS(voice="X")
    evil = "abc\u2019); Write-Output PWNED; #"
    try:
        eng.synthesize(evil)
    except Exception:
        pass
    assert "PWNED" not in seen["script"]
    assert seen["env"].get("F1TR_TTS_TEXT") == evil


def test_config_page_does_not_persist_masked_secret(tmp_path, monkeypatch):   # S7
    import config
    import config_ui
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=sk-REAL-SECRET-1234\n", encoding="utf-8")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(config, "_CONFIG", config.Config(env_path=env))
    page = {s["key"]: s["value"] for s in config_ui._settings_payload()}
    config_ui.apply_settings(page)            # what the page's save() posts back
    assert "LLM_API_KEY=sk-REAL-SECRET-1234" in env.read_text(encoding="utf-8")


def test_port_guard_sees_wildcard_holder():                  # M12
    import FI
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", 0))
        assert FI._port_busy(s.getsockname()[1]) is True
    finally:
        s.close()


def test_quali_time_picks_smallest_mark():                   # M3
    from radio_rules import RuleCtx, _rule_quali_time, build_default_rules
    build_default_rules()
    ctx = RuleCtx(prev=None, curr=None, now=0.0, settings=None, names=None,
                  new_events=[],
                  snapshot={"latest": {"session": {"session_time_left_s": 50}}})
    alert = _rule_quali_time(ctx)
    assert alert is not None and alert.dedup_key == "quali_time_60"


def test_race_model_single_lap_is_json_safe():               # S3
    from race_model import RaceModel
    m = RaceModel()
    m.update({"latest": {"session": {"session_kind": "race"}, "lap": {}},
              "lap_snapshots": [{"lap_num": 1, "lap_time_ms": 85000, "valid": True,
                                 "tyre_compound": "C3", "tyre_age_laps": 1}]}, 0.0)
    json.dumps(m.latest_dict(), allow_nan=False)
    assert m.latest.tyre_laps_to_limit is None


def test_all_declared_rules_are_registered():               # E (dead-code drift)
    import radio_rules as R
    ids = {r.id for r in R.build_default_rules()}
    declared = set(R._P0_RACE + R._P1_RACE + R._P2_RACE) | {
        "practice_long_run", "tt_new_pb", "quali_time", "quali_lap_done"}
    assert declared <= ids, sorted(declared - ids)
