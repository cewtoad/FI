"""Voice link orchestrator: audio in -> STT -> engineer.ask -> TTS out.

This is the R6 wiring (see PLAN_R6_VOICE.md). It deliberately touches nothing
else: the engineer, the state and both provider clients are injected, so the
whole chain is testable with stubs and STT/TTS stay swappable modules.

    browser PTT audio
        -> STTEngine.transcribe()          (stt_client)
        -> Engineer.ask(question, snap)    (existing Q&A path, unchanged)
        -> TTSEngine.synthesize(answer)    (tts_client, optional)
        -> {question, answer, audio_b64}   (rendered by the web UI)
"""

from __future__ import annotations

import base64
from typing import Any, Dict, Optional

from stt_client import STTEngine, make_stt
from tts_client import TTSEngine, make_tts


class VoiceLink:
    """One-shot voice question processing around the existing engineer."""

    def __init__(self, engineer: Any, state: Any,
                 stt: Optional[STTEngine] = None,
                 tts: Optional[TTSEngine] = None) -> None:
        self.engineer = engineer
        self.state = state
        self.stt = stt if stt is not None else make_stt()
        self.tts = tts if tts is not None else make_tts()

    # ------------------------------------------------------------ status

    @property
    def stt_available(self) -> bool:
        return self.stt is not None and self.stt.available

    @property
    def tts_available(self) -> bool:
        return self.tts is not None and self.tts.available

    @property
    def available(self) -> bool:
        """The link is usable when STT works; TTS is a nice-to-have."""
        return self.stt_available

    # ----------------------------------------------------------- process

    def process(self, audio: bytes, mime: str) -> Dict[str, Any]:
        """Transcribe a question, answer it, optionally synthesize the answer.

        Never raises: failures come back as {"error": ...} so the HTTP layer
        can always return JSON. TTS failure degrades to text-only.
        """
        if not self.stt_available:
            return {"error": "voice unavailable (STT off or not configured)"}
        try:
            question = (self.stt.transcribe(audio, mime) or "").strip()
        except Exception as e:  # noqa: BLE001 - surfaced as message
            return {"error": f"STT failed: {e}"}
        if not question:
            return {"error": "empty transcription"}

        snap = self.state.snapshot()
        answer = self.engineer.ask(question, snap)

        audio_b64: Optional[str] = None
        audio_mime: Optional[str] = None
        if self.tts_available:
            try:
                audio_bytes = self.tts.synthesize(answer)
                audio_b64 = base64.b64encode(audio_bytes).decode("ascii")
                audio_mime = self.tts.mime
            except Exception:  # text-only fallback, never fail the answer
                audio_b64 = None

        return {
            "question": question,
            "answer": answer,
            "audio_b64": audio_b64,
            "audio_mime": audio_mime,
        }
