"""Race-engineer Q&A engine: state snapshot -> DeepSeek answer."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ai_client import DeepSeekClient
from prompts import build_messages
from summariser import Summariser


class Engineer:
    """Answers driver questions using the latest telemetry summary."""

    MAX_HISTORY = 6

    def __init__(self, client: Optional[DeepSeekClient] = None,
                 summariser: Optional[Summariser] = None) -> None:
        self.client = client or DeepSeekClient()
        self.summariser = summariser or Summariser()
        self.history: List[Dict[str, str]] = []
        self.last_error: Optional[str] = None

    @property
    def configured(self) -> bool:
        return self.client.configured

    def ask(self, question: str, snapshot: Dict[str, Any]) -> str:
        """Answer a question against a raw TelemetryState snapshot.

        Returns the assistant text, or a short fallback message on error.
        """
        self.last_error = None
        summary = self.summariser.summarise(snapshot)
        messages = build_messages(question, summary, self.history)
        try:
            answer = self.client.chat(messages, temperature=0.3, max_tokens=400)
        except Exception as e:  # noqa: BLE001 - surface as message, never crash UI
            self.last_error = str(e)
            return f"[engine error] {e}"

        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        self.history = self.history[-self.MAX_HISTORY:]
        return answer

    def reset(self) -> None:
        self.history.clear()
