"""Voice packs and the TTS provider registry (T3.5).

Maps a user-selectable ``VoicePack`` (provider + engine voice id + tuning) to a
concrete TTS engine. The engine-specific rate/volume encoding is handled by a
per-provider adapter so callers never build engine strings.

Backward compatibility: ``make_tts()`` with no argument keeps the config-driven
behaviour (piper when a model is set, else sapi), so existing .env files and the
voice link keep working unchanged.

Local TTS is SAPI (system, WAV) + Piper (offline neural, WAV). Both emit WAV,
so the local audio path needs no MP3 decoder (edge-tts was removed: it only
emits MP3).
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Dict, List, Optional

from contracts import VoicePack

_log = logging.getLogger("f1_tr.voices")

# provider name -> factory(VoicePack) -> engine | None
PROVIDERS: Dict[str, Callable[[Optional[VoicePack]], object]] = {}


def register_provider(name: str, factory: Callable) -> None:
    PROVIDERS[name] = factory


def _list_sapi_voices() -> List[str]:
    """Enumerate installed SAPI voices via PowerShell (best-effort)."""
    if sys.platform != "win32":
        return []
    try:
        import subprocess
        script = ("Add-Type -AssemblyName System.Speech; "
                  "(New-Object System.Speech.Synthesis.SpeechSynthesizer)"
                  ".GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=10)
        if out.returncode == 0:
            return [v.strip() for v in out.stdout.decode("utf-8", "replace").splitlines()
                    if v.strip()]
    except Exception as e:  # noqa: BLE001
        _log.debug("SAPI voice enumeration failed: %r", e)
    return []


def list_voices() -> List[VoicePack]:
    """Return the available voice packs (SAPI + Piper if present)."""
    out: List[VoicePack] = []
    for name in _list_sapi_voices():
        out.append(VoicePack(id=f"sapi:{name}", provider="sapi", voice=name,
                             label=f"[SAPI] {name}"))
    # Piper: scan piper_models/*.onnx (offline neural voices).
    try:
        from paths import app_root
        model_dir = app_root() / "piper_models"
        if model_dir.is_dir():
            for p in sorted(model_dir.glob("*.onnx")):
                out.append(VoicePack(id=f"piper:{p.name}", provider="piper",
                                     voice=str(p), label=f"[Piper] {p.stem}"))
    except Exception:  # noqa: BLE001
        pass
    return out


def _make_sapi(pack: Optional[VoicePack]):
    from tts_client import SapiTTS
    return SapiTTS(voice=pack.voice if pack else None,
                   rate=pack.rate if pack else None)


def _make_piper(pack: Optional[VoicePack]):
    # piper-tts import name is `piper`; output is a real WAV (no MP3 decoder
    # needed). Voice = path to a .onnx model.
    from tts_client import PiperTTS
    return PiperTTS(voice=pack.voice if pack else None,
                    rate=pack.rate if pack else None)


def _make_default(pack: Optional[VoicePack]):
    from tts_client import make_tts as _legacy_make_tts
    return _legacy_make_tts()


register_provider("sapi", _make_sapi)
register_provider("piper", _make_piper)


def make_tts(pack: Optional[VoicePack] = None):
    """Build a TTS engine.

    No ``pack`` -> config-driven selection (piper else sapi), preserving the
    pre-T3 behaviour. With a pack -> that provider, or None if unavailable.
    """
    if pack is None:
        return _make_default(None)
    factory = PROVIDERS.get(pack.provider)
    if factory is None:
        return None
    engine = factory(pack)
    if engine is not None and getattr(engine, "available", False):
        return engine
    return None
