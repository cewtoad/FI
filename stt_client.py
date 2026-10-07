"""Speech-to-text (STT) clients for the voice link.

Providers (STT_PROVIDER in .env / environment):
    cloud - OpenAI-compatible POST /audio/transcriptions, stdlib only
    local - faster-whisper (OPTIONAL dependency, imported lazily)
    off   - disable STT (the voice link reports unavailable)

Unset -> auto-detect: cloud when STT_API_KEY / STT_BASE_URL are configured,
else local when faster-whisper is installed, else off.
See PLAN_R6_VOICE.md for the design rationale.
"""

from __future__ import annotations

import array
import io
import json
import os
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import get_config
from paths import app_root

# Models live beside the app (stt_models/), never in the C: HF cache. This is
# what makes the full pack self-contained / offline.
_MODELS_DIR = app_root() / "stt_models"
_STT_LIB = app_root() / "stt_lib"

_MIME_SUFFIX = {
    "audio/webm": ".webm",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
}


def build_multipart(fields: Dict[str, str], file_bytes: bytes,
                    filename: str, mime: str) -> Tuple[bytes, str]:
    """Build a multipart/form-data body using only the stdlib.

    Returns (body, content_type_header_value).
    """
    boundary = "----f1tr" + uuid.uuid4().hex
    parts: List[bytes] = []
    for name, value in fields.items():
        parts.append(
            (f"--{boundary}\r\n"
             f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
             f"{value}\r\n").encode("utf-8"))
    parts.append(
        (f"--{boundary}\r\n"
         f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
         f"Content-Type: {mime}\r\n\r\n").encode("utf-8")
        + file_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


class STTEngine:
    """Base class for swappable STT providers."""

    name = "base"

    @property
    def available(self) -> bool:
        raise NotImplementedError

    def transcribe(self, audio, mime: str = "") -> str:
        """Return the transcript for an audio blob ("" when nothing was said)."""
        raise NotImplementedError


class CloudSTT(STTEngine):
    """OpenAI-compatible /audio/transcriptions client (stdlib urllib only)."""

    name = "cloud"

    def __init__(self) -> None:
        cfg = get_config()
        self.api_key = cfg.get("STT_API_KEY").strip()
        self.base_url = cfg.get("STT_BASE_URL").strip().rstrip("/")
        self.model = cfg.get("STT_MODEL", "whisper-1").strip()
        self.language = cfg.get("STT_LANGUAGE", "zh").strip()
        self.timeout = cfg.get_float("STT_TIMEOUT", 30.0)

    @property
    def available(self) -> bool:
        return bool(self.api_key) and bool(self.base_url)

    def transcribe(self, audio, mime: str = "") -> str:
        """Raw mono float32 PCM (voice path) or encoded bytes (browser path)."""
        if not self.available:
            raise RuntimeError("STT not configured (STT_API_KEY / STT_BASE_URL)")
        if not isinstance(audio, (bytes, bytearray)):
            # Voice mode hands us raw PCM; the HTTP API wants an encoded file,
            # so wrap it as 16-bit WAV before building the multipart body.
            audio = pcm_to_wav_bytes(audio)
            mime = "audio/wav"
        mime = (mime or "audio/webm").split(";")[0].strip() or "audio/webm"
        suffix = _MIME_SUFFIX.get(mime, ".bin")
        body, ctype = build_multipart(
            {"model": self.model, "language": self.language},
            audio, f"audio{suffix}", mime)
        req = urllib.request.Request(
            f"{self.base_url}/audio/transcriptions",
            data=body, method="POST",
            headers={
                "Content-Type": ctype,
                "Authorization": f"Bearer {self.api_key}",
            })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"STT HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"STT network error: {e.reason}") from e
        return (data.get("text") or "").strip()


class LocalWhisperSTT(STTEngine):
    """faster-whisper local transcription. Optional dependency: the import is
    lazy so the app runs fine without it (available=False).

    CPU threads are capped (STT_LOCAL_THREADS, default 2) so the model never
    starves the game for CPU.
    """

    name = "local"

    def __init__(self, input_device: Optional[str] = None,
                 model_size: Optional[str] = None) -> None:
        # Make the bundled faster-whisper importable and keep model downloads
        # out of the C: drive / HF cache (project-local stt_lib + stt_models).
        if str(_STT_LIB) not in sys.path:
            sys.path.insert(0, str(_STT_LIB))
        os.environ.setdefault("HF_HOME", str(_MODELS_DIR))
        cfg = get_config()
        self.model_size = (model_size or cfg.get("STT_LOCAL_MODEL", "small")).strip()
        self.language = cfg.get("STT_LANGUAGE", "zh").strip()
        self.cpu_threads = cfg.get_int("STT_LOCAL_THREADS", 2)
        self._whisper = None
        self._model = None
        self._load_lock = threading.Lock()
        try:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel
        except Exception:  # optional dependency not installed
            self._whisper = None

    @property
    def available(self) -> bool:
        return self._whisper is not None

    def load(self) -> None:
        """Eagerly load the model (used by the voice preloader).

        download_root points at the bundled stt_models/, so with the model
        present this is fully offline; without it, it downloads there instead
        of the C: drive.
        """
        if self._model is None and self.available:
            # Single load even when the voice preloader and the first question
            # race (SenseVoice already had this lock; Whisper did not).
            with self._load_lock:
                if self._model is not None:
                    return
                # Force CPU int8: faster-whisper otherwise auto-selects CUDA and
                # fails on machines without cublas64_12.dll. CPU keeps the game's
                # GPU free and needs no CUDA install (see KNOWN_ISSUES ISSUE-3).
                self._model = self._whisper(
                    self.model_size, device="cpu", compute_type="int8",
                    cpu_threads=self.cpu_threads,
                    download_root=str(_MODELS_DIR))

    def transcribe(self, audio, mime: str = "") -> str:
        """Transcribe raw audio.

        Accepts either a float32 numpy array (in-memory PCM from the local
        voice stack) or encoded ``bytes`` (webm/wav from the browser PTT path).
        faster-whisper handles numpy directly; bytes are written to a temp file.
        """
        if not self.available:
            raise RuntimeError("faster-whisper not installed")
        self.load()  # no-op once loaded

        # numpy / list of samples -> transcribe in memory, no temp file.
        if not isinstance(audio, (bytes, bytearray)):
            segments, _info = self._model.transcribe(
                audio, language=self.language or None)
            return "".join(s.text for s in segments).strip()

        mime = (mime or "audio/webm").split(";")[0].strip()
        suffix = _MIME_SUFFIX.get(mime, ".bin")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            f.write(audio)
            path = f.name
        try:
            segments, _info = self._model.transcribe(
                path, language=self.language or None)
            return " ".join(s.text for s in segments).strip()
        finally:
            os.unlink(path)


class LocalSenseVoiceSTT(STTEngine):
    """Local SenseVoice-Small via sherpa-onnx (offline, no torch).

    Non-autoregressive: ~10x faster than whisper-small int8 on CPU (10s of
    audio in well under a second) and Chinese accuracy beats Whisper-Large
    (FunAudioLLM benchmarks). Optional dependency: sherpa-onnx bundled in
    stt_lib + model files in stt_models/sensevoice/ (model.int8.onnx +
    tokens.txt — see tools/download_sensevoice.py).
    """

    name = "sensevoice"

    def __init__(self, input_device: Optional[str] = None,
                 model_dir: Optional[str] = None) -> None:
        if str(_STT_LIB) not in sys.path:
            sys.path.insert(0, str(_STT_LIB))
        cfg = get_config()
        self.threads = max(1, cfg.get_int("STT_LOCAL_THREADS", 2))
        self._model_dir = Path(model_dir) if model_dir \
            else _MODELS_DIR / "sensevoice"
        self._recognizer = None
        self._load_lock = threading.Lock()

    def _lib(self):
        try:
            import sherpa_onnx  # optional, bundled in stt_lib
            return sherpa_onnx
        except Exception:
            return None

    @property
    def available(self) -> bool:
        if self._lib() is None:
            return False
        return ((self._model_dir / "model.int8.onnx").is_file()
                and (self._model_dir / "tokens.txt").is_file())

    def load(self) -> None:
        """Eagerly build the recognizer (voice preloader); idempotent, single
        load even when the preloader thread and the first question race."""
        if self._recognizer is not None or not self.available:
            return
        with self._load_lock:
            if self._recognizer is not None:
                return
            self._recognizer = self._lib().OfflineRecognizer.from_sense_voice(
                model=str(self._model_dir / "model.int8.onnx"),
                tokens=str(self._model_dir / "tokens.txt"),
                num_threads=self.threads,
                use_itn=True,
            )

    def transcribe(self, audio, mime: str = "") -> str:
        """float32/list PCM samples (voice path) or PCM-WAV bytes -> text."""
        if not self.available:
            raise RuntimeError(
                "SenseVoice local STT not installed (sherpa-onnx in stt_lib + "
                "model in stt_models/sensevoice — py -3.12 -m tools.download_sensevoice)")
        self.load()
        stream = self._recognizer.create_stream()
        rate = 16000
        if isinstance(audio, (bytes, bytearray)):
            rate, audio = _wav_bytes_to_f32(bytes(audio))
        stream.accept_waveform(rate, audio)
        self._recognizer.decode_streams([stream])
        return _strip_asr_tags(stream.result.text or "").strip()


def _strip_asr_tags(text: str) -> str:
    """SenseVoice rich output carries <|zh|><|NEUTRAL|><|Speech|>-style tags."""
    return re.sub(r"<\|[^|>]*\|>", "", text)


def pcm_to_wav_bytes(samples, rate: int = 16000) -> bytes:
    """Mono float32 samples (-1..1; list / array / numpy) -> 16-bit PCM WAV.

    CloudSTT uploads an encoded file, so the voice path's raw PCM must be
    wrapped before it reaches the HTTP layer. Values are clamped, so a hot
    signal cannot wrap around.
    """
    pcm = array.array("h", (
        max(-32768, min(32767, int(float(s) * 32767.0))) for s in samples))
    if sys.byteorder == "big":
        pcm.byteswap()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _wav_bytes_to_f32(data: bytes) -> List[float]:
    """PCM 16-bit WAV bytes -> float samples (-1..1) at the file's own rate."""
    with wave.open(io.BytesIO(data), "rb") as w:
        if w.getcomptype() != "NONE" or w.getsampwidth() != 2:
            raise RuntimeError(
                "SenseVoice local STT accepts PCM 16-bit WAV bytes only "
                "(voice mode passes raw PCM; browser audio needs cloud STT)")
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    arr = array.array("h")
    arr.frombytes(raw)
    if sys.byteorder == "big":
        arr.byteswap()
    samples = [s / 32768.0 for s in arr]
    return (rate, samples)


def _make_local(**kwargs) -> Optional[STTEngine]:
    """Build the configured local engine (STT_LOCAL_ENGINE).

    sensevoice (default) falls back to faster-whisper when sherpa-onnx or the
    model is missing, so a half-installed setup never kills the voice path.
    """
    cfg = get_config()
    engine = (cfg.get("STT_LOCAL_ENGINE", "sensevoice") or "").strip().lower()
    if engine in ("sensevoice", "auto", ""):
        sv = LocalSenseVoiceSTT(**kwargs)
        if sv.available:
            return sv
    wh = LocalWhisperSTT(**kwargs)
    return wh if wh.available else None


def make_stt(**kwargs) -> Optional[STTEngine]:
    """Pick the STT provider from config (explicit wins, else auto-detect).

    Extra kwargs (e.g. ``input_device``) are forwarded to the local provider.
    """
    cfg = get_config()
    provider = cfg.get("STT_PROVIDER", "").strip().lower()
    if provider == "off":
        return None
    if provider == "local":
        return _make_local(**kwargs)
    if provider == "cloud":
        eng = CloudSTT()
        return eng if eng.available else None
    # auto: cloud when configured, else local when installed, else off
    cloud = CloudSTT()
    if cloud.available:
        return cloud
    return _make_local(**kwargs)
