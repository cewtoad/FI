"""Quick TTS smoke test: speak a Chinese sentence on the configured output.

This is an audio smoke test, not an assertion test - it makes noise. It is
skipped unless RUN_NETWORK_TESTS=1 (grouped with the other loopback/audio
tests that need a real machine).
"""

from __future__ import annotations

import os

import pytest


def main() -> None:
    from voice_tts import LocalTTS

    t = LocalTTS(output_device="G733")
    print("available:", t.available)
    print("engine:", getattr(t.engine, "name", "?"))
    if not t.available:
        print("no TTS engine available; nothing to smoke-test")
        return
    t.speak("测试一下，我是你的赛车工程师。圈速比上一圈快零点三秒，继续保持。")
    print("done. error:", t.last_error)


@pytest.mark.network
def test_main() -> None:
    if os.environ.get("RUN_NETWORK_TESTS") != "1":
        pytest.skip("TTS smoke test needs audio (set RUN_NETWORK_TESTS=1)")
    main()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    main()
