"""Local TTS playback - compatibility shim over the unified audio outlet.

Historically this module owned both synthesis (via tts_client) and playback
(blocking ``sounddevice.play`` + ``wait``). As of T3.4 playback lives in
``speech.AudioPlayer`` (non-blocking, interruptible) and arbitration in
``speech.SpeechArbiter``.

``LocalTTS`` is kept as a thin shim so existing callers (voice_main,
voice_runner, tool_audio, tests) keep working with the same API: ``available``,
``engine``, ``speak(text)`` (still returns once playback finished for these
one-shot callers), and ``last_error``.
"""

from __future__ import annotations

import sys
import time
from typing import Optional

from paths import app_root

# Make stt_lib importable (sounddevice lives there).
_HERE = app_root()
if str(_HERE / "stt_lib") not in sys.path:
    sys.path.insert(0, str(_HERE / "stt_lib"))

import audio
from speech import AudioPlayer, UnsupportedAudioFormat
from tts_client import SapiTTS, make_tts
from voices import make_tts as make_tts_from_pack
from contracts import VoicePack


class LocalTTS:
    """Blocking-ish TTS facade backed by the shared AudioPlayer."""

    def __init__(self, output_device: str = "") -> None:
        self.output_device = output_device or audio.current()["output"]
        self.engine = make_tts() or SapiTTS()
        self.player = AudioPlayer(output_device=self.output_device)
        self.last_error: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.engine is not None and self.engine.available

    def speak(self, text: str) -> None:
        """Render text to speech and play it (waits for playback to finish)."""
        if not text:
            return
        try:
            audio_bytes = self.engine.synthesize(text)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            return
        try:
            self.player.play(audio_bytes, getattr(self.engine, "mime", "audio/wav"))
        except UnsupportedAudioFormat as e:
            self.last_error = str(e)
            return
        # Preserve the old blocking contract for these one-shot callers.
        while self.player.is_playing():
            time.sleep(0.02)
        self.last_error = self.player.last_error

    @property
    def _play_wav(self):
        """Back-compat alias: older code referenced this method name."""
        def _play(wav_bytes: bytes) -> None:
            self.player.play(wav_bytes, "audio/wav")
            while self.player.is_playing():
                time.sleep(0.02)
        return _play
