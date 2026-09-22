"""Race-engineer Q&A engine: state snapshot -> answer.

Flow:
    local router (deterministic, no LLM)
        -> hit: return immediately (zero latency / zero cost)
        -> miss: LLM via the active profile

The LLM client is resolved per request from config, so a runtime hot-swap
(``/api/llm``) takes effect on the next question without a restart.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from config import get_config
from llm_client import LLMError, make_llm
from profiles import LocalRouter, get_profile
from prompts import build_messages
from summariser import Summariser

# Shown when a question needs the LLM but no key is configured. Kept
# actionable: it names the fix and lists what still works without a key.
UNCONFIGURED_HINT = (
    "这个问题需要 AI,但你还没配置 key。"
    "点右上角【设置】填入 LLM_API_KEY 即可(或问本地能答的:"
    "名次 / 圈数 / 圈速 / 油量 / 胎温 / 轮胎 / 损伤 / 前车差距 / 进站)。"
)


class Engineer:
    """Answers driver questions using the latest telemetry summary."""

    def __init__(self, client: Any = None,
                 summariser: Optional[Summariser] = None,
                 config: Any = None) -> None:
        self._cfg = config or get_config()
        # An injected client pins the endpoint (used by tests); otherwise the
        # client is rebuilt from config on each ask so hot-swap works.
        self._client = client
        self.summariser = summariser or Summariser()
        self.router = LocalRouter()
        self.history: List[Dict[str, str]] = []
        self.last_error: Optional[str] = None
        self.last_usage: Optional[Dict[str, Any]] = None
        self.last_source: str = "none"  # "local" | "llm"
        self._lock = threading.Lock()
        self._busy = threading.Event()
        self._cancel = threading.Event()

    # ------------------------------------------------------------ client

    def active_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if getattr(self, "_cached", None) is None:
                self._cached = make_llm(self._cfg)
            return self._cached

    def refresh_client(self) -> None:
        """Drop the cached client so the next ask rebuilds it from config."""
        with self._lock:
            self._cached = None

    @property
    def client(self) -> Any:
        return self.active_client()

    @property
    def profile_name(self) -> str:
        return self._cfg.get("PROFILE", "") or "standard"

    @property
    def configured(self) -> bool:
        c = self.active_client()
        return bool(c is not None and c.configured)

    def describe(self) -> Dict[str, Any]:
        c = self.active_client()
        desc = c.describe() if c is not None else {}
        desc["profile"] = self.profile_name
        desc["local_router"] = self.router.stats()
        return desc

    # ------------------------------------------------------------ asking

    def cancel(self) -> None:
        """Signal an in-flight ask to abandon its result."""
        self._cancel.set()

    def ask(self, question: str, snapshot: Dict[str, Any]) -> str:
        """Answer a question against a raw snapshot.

        Fast path: the local router answers deterministic questions directly.
        Otherwise the LLM is used with the active profile.
        """
        self.last_error = None
        self._cancel.clear()
        profile = get_profile(self.profile_name)
        summary = self.summariser.summarise(snapshot)

        # 1) Deterministic local answer (no LLM).
        if profile.local_first:
            fast = self.router.answer(question, summary.get("facts", {}))
            if fast is not None:
                answer = fast.text
                self.last_source = "local"
                self.last_usage = None
                self._remember(question, answer, profile)
                return answer

        # 2) LLM path.
        client = self.active_client()
        if client is None or not client.configured:
            self.last_error = "LLM 未配置 (LLM_API_KEY / DEEPSEEK_API_KEY)"
            self.last_source = "no-key"
            return UNCONFIGURED_HINT

        messages = build_messages(question, summary, self.history, profile)
        try:
            answer = client.chat(messages,
                                 temperature=0.3,
                                 max_tokens=profile.max_tokens)
        except LLMError as e:
            self.last_error = str(e)
            return f"[engine error] {e}"
        except Exception as e:  # noqa: BLE001 - never crash the UI
            self.last_error = str(e)
            return f"[engine error] {e}"

        self.last_source = "llm"
        self.last_usage = getattr(client, "last_usage", None)
        self._remember(question, answer, profile)
        return answer

    def _remember(self, question: str, answer: str, profile) -> None:
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        self.history = self.history[-profile.max_history:]

    def reset(self) -> None:
        self.history.clear()
