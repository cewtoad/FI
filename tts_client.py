"""Text-to-speech (TTS) clients for the voice link.

Providers (TTS_PROVIDER in .env / environment):
    edge  - edge-tts (OPTIONAL dependency; neural voices; needs network)
    sapi  - Windows built-in System.Speech via PowerShell (zero deps, offline)
    piper - piper-tts (OPTIONAL, fully local/offline neural; needs a .onnx model)
    off   - disable TTS (answers stay text-only)

Unset -> auto-detect: edge when edge-tts is installed, else sapi on Windows,
else off. Runtime synthesis failures degrade to text-only in voice.py.
See PLAN_R6_VOICE.md for the design rationale.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from config import get_config


def _sapi_rate(rate) -> Optional[int]:
    """Coerce a rate into SAPI's -10..10 integer (T3.5 conversion layer).

    Percent-style values ("+10%") are edge's -100..100 scale, divided by 10 to
    SAPI's -10..10. Bare integers are treated as already-SAPI-scale.
    """
    if rate is None or rate == "":
        return None
    text = str(rate).strip()
    percent = text.endswith("%")
    try:
        v = int(float(text.replace("%", "").replace("+", "")))
    except (TypeError, ValueError):
        return None
    if percent:
        v = v // 10
    return max(-10, min(10, v))


def _edge_rate(rate) -> Optional[str]:
    """Coerce a rate into edge's "+N%" string."""
    if rate is None or rate == "":
        return None
    text = str(rate).strip()
    if text.endswith("%"):
        return text if text.startswith(("+", "-")) else f"+{text}"
    try:
        v = int(float(text))
    except (TypeError, ValueError):
        return None
    return f"{'+' if v >= 0 else ''}{v * 10}%"


class TTSEngine:
    """Base class for swappable TTS providers."""

    name = "base"
    mime = "application/octet-stream"

    @property
    def available(self) -> bool:
        raise NotImplementedError

    def synthesize(self, text: str) -> bytes:
        """Render text to audio bytes in self.mime format."""
        raise NotImplementedError


class EdgeTTS(TTSEngine):
    """edge-tts (Microsoft Edge neural voices, e.g. zh-CN-XiaoxiaoNeural).

    Optional dependency: imported lazily; without it available=False.
    asyncio.run() is safe here because synthesize() is called from worker
    threads (ThreadingHTTPServer), never from a running event loop.
    """

    name = "edge"
    mime = "audio/mpeg"

    def __init__(self, voice: Optional[str] = None,
                 rate=None) -> None:
        cfg = get_config()
        self.voice = (voice or cfg.get("TTS_VOICE", "zh-CN-XiaoxiaoNeural")).strip()
        self.rate = _edge_rate(rate)
        try:
            import edge_tts
        except Exception:  # optional dependency not installed
            self._edge_tts = None
        else:
            self._edge_tts = edge_tts

    @property
    def available(self) -> bool:
        return self._edge_tts is not None

    def synthesize(self, text: str) -> bytes:
        if not self.available:
            raise RuntimeError("edge-tts not installed")
        import asyncio

        async def _run() -> bytes:
            kwargs = {}
            if self.rate:
                kwargs["rate"] = self.rate
            communicate = self._edge_tts.Communicate(text, self.voice, **kwargs)
            buf = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.extend(chunk["data"])
            if not buf:
                raise RuntimeError("edge-tts returned no audio")
            return bytes(buf)

        return asyncio.run(_run())


class SapiTTS(TTSEngine):
    """Windows built-in speech via PowerShell + System.Speech (zero deps).

    Pragmatic zero-dependency fallback: spawns powershell.exe, renders to a
    temporary WAV, returns the bytes (played in the browser like any other
    engine). Voice depends on the Windows language packs installed.
    """

    name = "sapi"
    mime = "audio/wav"

    def __init__(self, voice: Optional[str] = None, rate=None) -> None:
        cfg = get_config()
        self.voice = (voice or cfg.get("TTS_SAPI_VOICE", "")).strip()
        self.timeout = float(cfg.get("TTS_TIMEOUT", "30"))
        self.rate = _sapi_rate(rate)

    @property
    def available(self) -> bool:
        return sys.platform == "win32"

    def synthesize(self, text: str) -> bytes:
        if not self.available:
            raise RuntimeError("SAPI TTS is Windows-only")
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory(prefix="f1tr_tts_") as td:
            out = os.path.join(td, "out.wav")
            # PowerShell single-quoted strings escape ' as ''
            safe_voice = self.voice.replace("'", "''")
            safe_text = text.replace("'", "''").replace("\r", " ").replace("\n", " ")
            select = (f"try {{ $s.SelectVoice('{safe_voice}') }} catch {{ }}"
                      if self.voice else "")
            set_rate = (f"try {{ $s.Rate = {self.rate} }} catch {{ }}"
                        if self.rate is not None else "")
            script = (
                "Add-Type -AssemblyName System.Speech\n"
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                f"{select}\n"
                f"{set_rate}\n"
                f"$s.SetOutputToWaveFile('{out}')\n"
                f"$s.Speak('{safe_text}')\n"
                "$s.Dispose()\n")
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, timeout=self.timeout)
            if proc.returncode != 0 or not os.path.exists(out):
                err = proc.stderr.decode("utf-8", errors="replace")[:200]
                raise RuntimeError(f"SAPI TTS failed: {err}")
            with open(out, "rb") as f:
                return f.read()


class PiperTTS(TTSEngine):
    """Piper neural TTS - fully local / offline (T3.5 / stop point #4 resolved).

    piper-tts (import ``piper``) runs an ONNX voice model on CPU. Chinese needs
    the ``[zh]`` extra (g2pW phonemizer). Output is a real WAV, so it plays
    through AudioPlayer with no MP3 decoder needed.

    ``voice`` is the path to a ``.onnx`` model (its ``.onnx.json`` config is
    auto-discovered alongside it). The model is loaded lazily on first use
    because loading takes a moment.
    """

    name = "piper"
    mime = "audio/wav"

    def __init__(self, voice: Optional[str] = None, rate=None) -> None:
        cfg = get_config()
        self.voice_path = (voice or cfg.get("TTS_PIPER_VOICE", "")).strip()
        # length_scale > 1 is slower; map our -10..10 rate to roughly 1 + rate/20.
        self.length_scale = _piper_length_scale(rate)
        self._piper = None
        self._voice_obj = None
        try:
            import piper  # noqa: F401
            self._piper = piper
        except Exception:
            self._piper = None

    @property
    def available(self) -> bool:
        if self._piper is None:
            return False
        import os
        return bool(self.voice_path) and os.path.exists(self.voice_path)

    def synthesize(self, text: str) -> bytes:
        if not self.available:
            raise RuntimeError("piper not installed or TTS_PIPER_VOICE not set")
        import io
        import wave

        voice = self._ensure_voice()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav_file:
            voice.synthesize_wav(text, wav_file)
        return buf.getvalue()

    def _ensure_voice(self):
        if self._voice_obj is None:
            self._voice_obj = self._piper.PiperVoice.load(self.voice_path)
        return self._voice_obj


def _piper_length_scale(rate) -> Optional[float]:
    """Map a rate into Piper's length_scale (1.0 = normal, >1 slower)."""
    if rate is None or rate == "":
        return None
    try:
        v = int(float(str(rate).replace("%", "").replace("+", "")))
    except (TypeError, ValueError):
        return None
    return max(0.5, min(2.0, 1.0 - v / 20.0))


def make_tts() -> Optional[TTSEngine]:
    """Pick the TTS provider from config (explicit wins, else auto-detect)."""
    cfg = get_config()
    provider = cfg.get("TTS_PROVIDER", "").strip().lower()
    if provider == "off":
        return None
    if provider == "edge":
        eng: TTSEngine = EdgeTTS()
        return eng if eng.available else None
    if provider == "sapi":
        eng = SapiTTS()
        return eng if eng.available else None
    if provider == "piper":
        eng = PiperTTS()
        return eng if eng.available else None
    # auto: edge when installed, else sapi on Windows, else off
    edge = EdgeTTS()
    if edge.available:
        return edge
    sapi = SapiTTS()
    return sapi if sapi.available else None
