"""Local speech-to-text using faster-whisper (CPU int8).

Model and libs live inside the project folder (stt_lib / stt_models) so nothing
touches the C: drive.

Public API:
    LocalSTT.available            -> bool
    LocalSTT.transcribe(audio)    -> str   (audio: float32 mono @ 16kHz numpy)
    LocalSTT.record(seconds)      -> np.ndarray  (from the configured mic)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import audio
from paths import app_root

HERE = app_root()
_SYS_PATH_ADDED = False


def _ensure_paths() -> None:
    global _SYS_PATH_ADDED
    if not _SYS_PATH_ADDED:
        sys.path.insert(0, str(HERE / "stt_lib"))
        os.environ.setdefault("HF_HOME", str(HERE / "stt_models"))
        _SYS_PATH_ADDED = True


SR = 16000
CHANNELS = 1


class LocalSTT:
    def __init__(self, model_size: str = "small", input_device: str = "",
                 language: str = "zh") -> None:
        _ensure_paths()
        self.model_size = model_size
        self.input_device = input_device or audio.current()["input"]
        self.language = language
        self._model = None
        self._sd = None
        self.last_error: Optional[str] = None

    @property
    def available(self) -> bool:
        try:
            _ensure_paths()
            import faster_whisper  # noqa: F401
            import sounddevice  # noqa: F401
            return True
        except Exception as e:  # noqa: BLE001
            self.last_error = str(e)
            return False

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(
                self.model_size, device="cpu", compute_type="int8",
                download_root=str(HERE / "stt_models"))
        return self._model

    def record(self, seconds: float):
        """Blocking record from the configured mic; returns float32 mono array."""
        import sounddevice as sd
        dev = audio.active_device("input", self.input_device or None)
        if dev["id"] is None:
            raise RuntimeError("没有可用的输入设备（未连接麦克风？）")
        pcm = sd.rec(int(seconds * SR), samplerate=SR, channels=CHANNELS,
                     dtype="float32", device=dev["id"])
        sd.wait()
        return pcm.flatten()

    def transcribe(self, pcm) -> str:
        model = self._load()
        segments, _info = model.transcribe(
            pcm, language=self.language, beam_size=5, vad_filter=True)
        return "".join(seg.text for seg in segments).strip()


class StreamingRecorder:
    """Records in the background; start()/stop() around a spoken question.

    Lets the caller begin recording on a key press and stop on the next press,
    without blocking.
    """

    #: below this many samples the recording is treated as empty/too short
    MIN_SAMPLES = int(SR * 0.3)

    def __init__(self, input_device: str = "", max_seconds: float = 10.0) -> None:
        _ensure_paths()
        self.input_device = input_device or audio.current()["input"]
        self.max_seconds = max_seconds
        self._stream = None
        self._frames = []
        self._recording = False
        self.last_error: Optional[str] = None

    def start(self) -> None:
        import sounddevice as sd
        self._frames = []
        self._recording = True
        # Resolve the device to use for THIS take: pinned fragment if it
        # matches, else the system's current default (re-queried each start,
        # so switching headsets / the Windows default is picked up live).
        dev = audio.active_device("input", self.input_device or None)
        if dev["id"] is None:
            self._recording = False
            self.last_error = "没有可用的输入设备（未连接麦克风？）"
            raise RuntimeError(self.last_error)
        tag = "已指定" if dev["source"] == "pinned" else "跟随系统当前设备"
        print(f"[voice] 麦克风: {dev['name']}（{tag}）", flush=True)
        idx = dev["id"]

        def _cb(indata, frames, time_info, status):
            if self._recording:
                self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=SR, channels=CHANNELS, dtype="float32",
            device=idx, callback=_cb)
        self._stream.start()

    def stop(self):
        import numpy as np
        self._recording = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        if not self._frames:
            return np.zeros(0, dtype="float32")
        return np.concatenate(self._frames).flatten()

    def collected_samples(self) -> int:
        """Number of samples captured so far (for the too-short guard)."""
        return sum(len(f) for f in self._frames)
