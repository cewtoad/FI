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

import json
import os
import tempfile
import urllib.error
import urllib.request
import uuid
from typing import Dict, List, Optional, Tuple

from config import get_config

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

    def transcribe(self, audio: bytes, mime: str) -> str:
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

    def transcribe(self, audio: bytes, mime: str) -> str:
        if not self.available:
            raise RuntimeError("STT not configured (STT_API_KEY / STT_BASE_URL)")
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
        cfg = get_config()
        self.model_size = (model_size or cfg.get("STT_LOCAL_MODEL", "small")).strip()
        self.language = cfg.get("STT_LANGUAGE", "zh").strip()
        self.cpu_threads = cfg.get_int("STT_LOCAL_THREADS", 2)
        self._whisper = None
        self._model = None
        try:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel
        except Exception:  # optional dependency not installed
            self._whisper = None

    @property
    def available(self) -> bool:
        return self._whisper is not None

    def transcribe(self, audio: bytes, mime: str) -> str:
        if not self.available:
            raise RuntimeError("faster-whisper not installed")
        if self._model is None:  # lazy model load keeps startup fast
            self._model = self._whisper(
                self.model_size, compute_type="int8",
                cpu_threads=self.cpu_threads)
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


def make_stt(**kwargs) -> Optional[STTEngine]:
    """Pick the STT provider from config (explicit wins, else auto-detect).

    Extra kwargs (e.g. ``input_device``) are forwarded to the local provider.
    """
    cfg = get_config()
    provider = cfg.get("STT_PROVIDER", "").strip().lower()
    if provider == "off":
        return None
    if provider == "local":
        eng: STTEngine = LocalWhisperSTT(**kwargs)
        return eng if eng.available else None
    if provider == "cloud":
        eng = CloudSTT()
        return eng if eng.available else None
    # auto: cloud when configured, else local when installed, else off
    cloud = CloudSTT()
    if cloud.available:
        return cloud
    local = LocalWhisperSTT(**kwargs)
    return local if local.available else None
