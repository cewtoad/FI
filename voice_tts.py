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
from paths import app_root

_HERE = app_root()
if str(_HERE / "stt_lib") not in sys.path:
    sys.path.insert(0, str(_HERE / "stt_lib"))

import audio
from tts_client import SapiTTS, make_tts


def _resample(data, sr_in: int, sr_out: int):
    """Linear-interpolation resample (numpy only; no scipy dependency)."""
    import numpy as np

    if sr_in == sr_out or data.size == 0:
        return data
    n_out = int(round(data.shape[0] * sr_out / sr_in))
    if n_out <= 0:
        return data
    src = np.linspace(0, data.shape[0] - 1, n_out)
    if data.ndim == 1:
        return np.interp(src, np.arange(data.shape[0]), data).astype(np.float32)
    cols = [np.interp(src, np.arange(data.shape[0]), data[:, c]) for c in range(data.shape[1])]
    return np.column_stack(cols).astype(np.float32)


class LocalTTS:
    def __init__(self, output_device: str = "") -> None:
        self.output_device = output_device or audio.current()["output"]
        self.engine = make_tts() or SapiTTS()
        self.last_error: Optional[str] = None

    @property
    def available(self) -> bool:
        return self.engine is not None and self.engine.available

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

        idx = audio.resolve(self.output_device, "output")
        # Many gaming headsets expose 4-8 output channels at 44.1/48kHz. Playing
        # a mono WAV at 22.05kHz at them can silently go to the wrong channel or
        # fail to resample. Render to the device's preferred samplerate and
        # duplicate to 2 channels, which every output device accepts.
        try:
            info = sd.query_devices(idx) if idx is not None else sd.query_devices(kind="output")
            dev_sr = int(info.get("default_samplerate") or sr)
            max_out = int(info.get("max_output_channels") or 2)
        except Exception:
            dev_sr, max_out = sr, 2

        if dev_sr != sr:
            data = _resample(data, sr, dev_sr)
        if data.ndim == 1 and max_out >= 2:
            data = np.column_stack([data, data])
        elif data.ndim == 2 and data.shape[1] == 1 and max_out >= 2:
            data = np.repeat(data, 2, axis=1)

        try:
            dev_name = sd.query_devices(idx)["name"] if idx is not None else "(system default)"
        except Exception:
            dev_name = str(idx)
        print(f"[tts] 播放到 [{idx}] {dev_name} @ {dev_sr}Hz "
              f"{'x'.join(map(str, data.shape))}", flush=True)

        self.last_error = None
        try:
            sd.play(data, dev_sr, device=idx)
            sd.wait()
        except Exception as e:  # noqa: BLE001
            self.last_error = f"playback failed: {e}"
            # Last resort: let PortAudio pick the default device.
            try:
                sd.play(data, dev_sr)
                sd.wait()
            except Exception as e2:  # noqa: BLE001
                self.last_error = f"playback failed: {e2}"
