"""Text-to-speech (TTS) clients for the voice link.

Providers (TTS_PROVIDER in .env / environment):
    edge - edge-tts (OPTIONAL dependency; neural voices; needs network)
    sapi - Windows built-in System.Speech via PowerShell (zero deps, offline)
    off  - disable TTS (answers stay text-only)

Unset -> auto-detect: edge when edge-tts is installed, else sapi on Windows,
else off. Runtime synthesis failures degrade to text-only in voice.py.
See PLAN_R6_VOICE.md for the design rationale.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from config import get_config


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

    def __init__(self) -> None:
        cfg = get_config()
        self.voice = cfg.get("TTS_VOICE", "zh-CN-XiaoxiaoNeural").strip()
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
            communicate = self._edge_tts.Communicate(text, self.voice)
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

    def __init__(self) -> None:
        cfg = get_config()
        self.voice = cfg.get("TTS_SAPI_VOICE", "").strip()
        self.timeout = float(cfg.get("TTS_TIMEOUT", "30"))

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
            script = (
                "Add-Type -AssemblyName System.Speech\n"
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                f"{select}\n"
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
    # auto: edge when installed, else sapi on Windows, else off
    edge = EdgeTTS()
    if edge.available:
        return edge
    sapi = SapiTTS()
    return sapi if sapi.available else None
