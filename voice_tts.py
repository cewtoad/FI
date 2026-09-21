"""Local TTS playback (Windows SAPI) using the project's TTS client.

Reuses tts_client.SapiTTS to render WAV bytes, then plays them synchronously on
the configured output device (the G733 headset), so the race engineer's answer
comes out of the headset while the game keeps running.
"""

from __future__ import annotations

import io
import os
import sys
import wave
from pathlib import Path
from typing import Optional

# Make stt_lib importable (sounddevice lives there).
_HERE = Path(__file__).parent
if str(_HERE / "stt_lib") not in sys.path:
    sys.path.insert(0, str(_HERE / "stt_lib"))

from tts_client import SapiTTS, make_tts


class LocalTTS:
    def __init__(self, output_device: str = "G733") -> None:
        self.output_device = output_device
        self.engine = make_tts() or SapiTTS()
        self.last_error: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.engine is not None and self.engine.available

    def _device_index(self):
        import sounddevice as sd
        for i, d in enumerate(sd.query_devices()):
            if self.output_device.lower() in d["name"].lower() \
                    and d["max_output_channels"] > 0:
                return i
        return None

    def speak(self, text: str) -> None:
        """Render text to speech and play it on the output device (blocking)."""
        if not text:
            return
        try:
            wav_bytes = self.engine.synthesize(text)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            return
        self._play_wav(wav_bytes)

    def _play_wav(self, wav_bytes: bytes) -> None:
        import numpy as np
        import sounddevice as sd

        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            sr = w.getframerate()
            n = w.getnframes()
            ch = w.getnchannels()
            raw = w.readframes(n)
        data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if ch > 1:
            data = data.reshape(-1, ch)
        sd.play(data, sr, device=self._device_index())
        sd.wait()
