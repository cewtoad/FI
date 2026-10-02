"""Tests for the TTS text-normalization layer (tts_text.normalize_for_tts)."""

from tts_text import normalize_for_tts as n


def test_lap_times():
    assert n("上一圈 1:31.204") == "上一圈 1分31秒204"
    assert n("最快圈是你：1:29.877。") == "最快圈是你：1分29秒877。"
    # H:MM:SS clocks (no .mmm) and plain times must stay untouched.
    assert n("用时 1:23:45") == "用时 1:23:45"
    assert n("还剩 8 圈") == "还剩 8 圈"


def test_gaps_seconds():
    assert n("距前车 落后 2.1s") == "距前车 落后 2.1秒"
    assert n("落后领先者 19.982s") == "落后领先者 19.982秒"
    assert n("差 8s") == "差 8秒"


def test_temps_and_slashes():
    assert n("胎温 98/101/97C") == "胎温 98、101、97度"
    assert n("胎温 98 C") == "胎温 98度"


def test_positions():
    assert n("你 P5") == "你 P五"
    assert n("掉到 P12") == "掉到 P十二"
    assert n("最终第 P20位") == "最终第 P二十位"
    # CJK-adjacent (no space) — \b would miss these, lookarounds must not.
    assert n("掉到P12") == "掉到P十二"
    assert n("胎温97C") == "胎温97度"
    assert n("落后2.1s") == "落后2.1秒"
    # Non-position tokens are untouched.
    assert n("DRS 故障") == "DRS 故障"
    assert n("undercut 风险") == "undercut 风险"
    assert n("WP5 不算名次") == "WP5 不算名次"


def test_percent():
    assert n("车损已达 24%，下压力会受影响。") == "车损已达 百分之24，下压力会受影响。"
    assert n("降水概率 30%") == "降水概率 百分之30"


def test_misc_unchanged():
    assert n("undercut 风险") == "undercut 风险"
    assert n("预计 8 分钟后降雨") == "预计 8 分钟后降雨"
    assert n("") == ""
    assert n("安全车出动。") == "安全车出动。"


def test_arbiter_synthesizes_normalized_text_only():
    """Normalization happens at synthesis time; the queue text stays original."""
    import time as _t

    from contracts import Utterance
    from speech import SpeechArbiter

    class FakeTTS:
        available = True
        mime = "audio/wav"

        def __init__(self):
            self.texts = []

        def synthesize(self, text):
            self.texts.append(text)
            return b"wav"

    class FakePlayer:
        def play(self, audio, mime):
            pass

        def stop(self):
            pass

        def is_playing(self):
            return False

    tts = FakeTTS()
    arb = SpeechArbiter(tts, FakePlayer(), clock=lambda: 0.0)
    arb.start()
    try:
        utt = Utterance(text="上一圈 1:31.204，你 P5，胎温 98C",
                        priority=2, source="answer",
                        created_at=0.0, gated=False)
        arb.submit(utt)
        end = _t.time() + 2.0
        while _t.time() < end and arb.spoken < 1:
            _t.sleep(0.01)
        assert tts.texts == ["上一圈 1分31秒204，你 P五，胎温 98度"]
        # The utterance object keeps the original text (display/reports).
        assert utt.text == "上一圈 1:31.204，你 P5，胎温 98C"
    finally:
        arb.stop()
