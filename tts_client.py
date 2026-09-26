"""Text-to-speech (TTS) clients for the voice link.

Providers (TTS_PROVIDER in .env / environment):
    sapi  - Windows built-in System.Speech via PowerShell (zero deps, offline)
    piper - piper-tts (OPTIONAL, fully local/offline neural; needs a .onnx model)
    off   - disable TTS (answers stay text-only)

Unset -> auto-detect: piper when a voice model is configured, else sapi on
Windows, else off. Runtime synthesis failures degrade to text-only in voice.py.

Both providers emit real WAV, so the local AudioPlayer needs no MP3 decoder
(edge-tts was removed: it only emits MP3 and would have required a decoder).
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from config import get_config


def _sapi_rate(rate) -> Optional[int]:
    """Coerce a rate into SAPI's -10..10 integer (T3.5 conversion layer).

    Percent-style values ("+10%") are on a -100..100 scale, divided by 10 to
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


def _pick_chinese_sapi_voice() -> Optional[str]:
    """Return an installed Chinese SAPI voice name, or None (best-effort)."""
    if sys.platform != "win32":
        return None
    names = ("huihui", "yaoyao", "kangkang", "lili", "chinese", "zh-")
    try:
        import subprocess
        script = ("Add-Type -AssemblyName System.Speech; "
                  "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                  ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=10)
        if out.returncode != 0:
            return None
        for line in out.stdout.decode("utf-8", "replace").splitlines():
            name = line.strip()
            if name and any(n in name.lower() for n in names):
                return name
    except Exception:  # noqa: BLE001
        return None
    return None


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


class SapiTTS(TTSEngine):
    """Windows built-in speech via PowerShell + System.Speech (zero deps).

    Pragmatic zero-dependency fallback: spawns powershell.exe, renders to a
    temporary WAV, returns the bytes (played locally like any other engine).
    Voice depends on the Windows language packs installed.
    """

    name = "sapi"
    mime = "audio/wav"

    def __init__(self, voice: Optional[str] = None, rate=None) -> None:
        cfg = get_config()
        self.voice = (voice or cfg.get("TTS_SAPI_VOICE", "")).strip()
        # No explicit voice: prefer an installed Chinese voice so the (Chinese)
        # race-engineer speech is intelligible instead of read by an English
        # voice. Falls back to the system default when none is found.
        if not self.voice:
            self.voice = _pick_chinese_sapi_voice() or ""
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
            # Untrusted text (online player names, LLM output) is passed via
            # environment variables, never spliced into the script: PowerShell
            # also treats U+2018/U+2019/U+201A/U+201B as single quotes, so the
            # old ''-escaping allowed breaking out of the string literal.
            set_rate = (f"try {{ $s.Rate = {int(self.rate)} }} catch {{ }}"
                        if self.rate is not None else "")
            script = (
                "Add-Type -AssemblyName System.Speech\n"
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
                "if ($env:F1TR_TTS_VOICE) { try { $s.SelectVoice($env:F1TR_TTS_VOICE) } catch { } }\n"
                f"{set_rate}\n"
                "$s.SetOutputToWaveFile($env:F1TR_TTS_OUT)\n"
                "$s.Speak($env:F1TR_TTS_TEXT)\n"
                "$s.Dispose()\n")
            env = dict(os.environ,
                       F1TR_TTS_TEXT=text.replace("\r", " ").replace("\n", " "),
                       F1TR_TTS_VOICE=self.voice or "", F1TR_TTS_OUT=out)
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive",
                 "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True, timeout=self.timeout, env=env)
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
        # piper-tts lives in the bundled stt_lib/ (installed with --target), so
        # add it to sys.path here the same way stt_client does for whisper.
        try:
            import sys as _sys

            from paths import app_root
            lib = app_root() / "stt_lib"
            if lib.is_dir() and str(lib) not in _sys.path:
                _sys.path.insert(0, str(lib))
        except Exception:  # noqa: BLE001
            pass
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
            # Point download_dir at piper_models/ so the Chinese g2pW resource
            # (needed for the pinyin phonemizer) is looked up next to the voice
            # model instead of in the process CWD.
            download_dir = None
            try:
                from paths import app_root
                dl = app_root() / "piper_models"
                if dl.is_dir():
                    download_dir = str(dl)
            except Exception:  # noqa: BLE001
                pass
            if download_dir:
                self._voice_obj = self._piper.PiperVoice.load(
                    self.voice_path, download_dir=download_dir)
            else:
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
    if provider == "sapi":
        eng = SapiTTS()
        return eng if eng.available else None
    if provider == "piper":
        eng = PiperTTS()
        return eng if eng.available else None
    # auto: prefer sapi (zero deps, instant) over piper. Piper is opt-in only
    # (TTS_PROVIDER=piper) because its Chinese pinyin path needs the large g2pW
    # resource and was not reliable on every machine; making it the default
    # would risk a slow/hanging synth. Explicit TTS_PROVIDER=piper still works.
    sapi = SapiTTS()
    if sapi.available:
        return sapi
    piper = PiperTTS()
    return piper if piper.available else None
