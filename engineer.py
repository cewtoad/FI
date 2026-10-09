"""Race-engineer Q&A engine: state snapshot -> answer.

Flow:
    local router (deterministic, no LLM)
        -> hit: return immediately (zero latency / zero cost)
        -> miss: LLM via the active profile

The LLM client is resolved lazily from config and cached; ``refresh_client()``
drops the cache so a runtime hot-swap (``/api/llm``, or the ticker's config
reload) takes effect on the next question without a restart.
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
# T1.1: the web and voice channels point at the right place to set the key
# (web UI settings panel vs the standalone config page).
UNCONFIGURED_HINT = (
    "这个问题需要 AI,但你还没配置 key。"
    "点右上角【设置】填入 LLM_API_KEY 即可(或问本地能答的:"
    "名次 / 圈数 / 圈速 / 油量 / 胎温 / 轮胎 / 损伤 / 前车差距 / 进站)。"
)
UNCONFIGURED_HINT_VOICE = (
    "这个问题需要 AI,但你还没配置 key。"
    "运行 py -3.12 FI.py --config 填入 LLM_API_KEY 即可"
    "(或问本地能答的:名次 / 圈数 / 圈速 / 油量 / 胎温 / 轮胎 / 损伤 / 前车差距 / 进站)。"
)


def unconfigured_hint(channel: str) -> str:
    """Pick the no-key hint that matches the caller's channel."""
    if (channel or "").strip().lower() == "voice":
        return UNCONFIGURED_HINT_VOICE
    return UNCONFIGURED_HINT


class Engineer:
    """Answers driver questions using the latest telemetry summary."""

    def __init__(self, client: Any = None,
                 summariser: Optional[Summariser] = None,
                 config: Any = None) -> None:
        self._cfg = config or get_config()
        # An injected client pins the endpoint (used by tests); otherwise the
        # client is built from config once and cached — refresh_client()
        # invalidates it so config hot-swaps take effect.
        self._client = client
        self.summariser = summariser or Summariser()
        self.router = LocalRouter()
        self.history: List[Dict[str, str]] = []
        self.last_error: Optional[str] = None
        self.last_usage: Optional[Dict[str, Any]] = None
        self.last_source: str = "none"  # "local" | "llm"
        self._lock = threading.Lock()
        # Serialises ask / history so web text + web voice (and any other
        # caller) cannot interleave turns on the shared history list.
        self._ask_lock = threading.Lock()
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

    def _names(self):
        """Lazily build a NameRenderer from config (for rival-pace lookup)."""
        with self._lock:
            if getattr(self, "_name_renderer", None) is None:
                try:
                    from names import NameRenderer
                    style = self._cfg.get("DRIVER_NAME_STYLE", "zh") or "zh"
                    self._name_renderer = NameRenderer(style=style)
                except Exception:  # noqa: BLE001
                    self._name_renderer = None
            return self._name_renderer

    def describe(self) -> Dict[str, Any]:
        c = self.active_client()
        desc = c.describe() if c is not None else {}
        desc["profile"] = self.profile_name
        desc["local_router"] = self.router.stats()
        return desc

    # ------------------------------------------------------------ asking

    def cancel(self) -> None:
        """Signal an in-flight ask to abandon its result.

        Checked between stages (the synchronous urllib call itself cannot be
        interrupted): before the LLM call is started and after it returns, so
        a finished-but-cancelled answer is discarded.
        """
        self._cancel.set()

    def ask(self, question: str, snapshot: Dict[str, Any],
            channel: str = "web") -> str:
        """Answer a question against a raw snapshot.

        Fast path: the local router answers deterministic questions directly.
        Otherwise the LLM is used with the active profile.

        ``channel`` only affects the no-key hint wording (web vs voice); the
        local fast path always runs first, regardless of key.

        Asks are serialised on ``_ask_lock`` so concurrent callers (web text,
        web voice, in-game voice) cannot interleave history / last_* fields.
        """
        with self._ask_lock:
            return self._ask_unlocked(question, snapshot, channel)

    def _ask_unlocked(self, question: str, snapshot: Dict[str, Any],
                      channel: str) -> str:
        self.last_error = None
        self._cancel.clear()
        profile = get_profile(self.profile_name)
        summary = self.summariser.summarise(snapshot)

        # 1) Deterministic local answer (no LLM).
        if profile.local_first:
            fast = self.router.answer(question, summary.get("facts", {}),
                                      leaderboard=summary.get("leaderboard"),
                                      name_renderer=self._names())
            if fast is not None:
                answer = fast.text
                self.last_source = "local"
                self.last_usage = None
                self._remember(question, answer, profile)
                return answer

        # 2) LLM path.
        if self._cancel.is_set():
            # cancel() arrived while summarising / routing: don't start the call.
            self.last_error = None
            self.last_source = "cancelled"
            return "[cancelled]"
        client = self.active_client()
        if client is None or not client.configured:
            self.last_error = "LLM 未配置 (LLM_API_KEY / DEEPSEEK_API_KEY)"
            self.last_source = "no-key"
            return unconfigured_hint(channel)

        # Snapshot history under the ask lock (already held) so the message
        # list is a stable copy for this turn.
        history_snap = list(self.history)
        messages = build_messages(question, summary, history_snap, profile)
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
        if self._cancel.is_set():
            # The call finished but was cancelled meanwhile: drop the result
            # (it must not enter history or the UI).
            self.last_source = "cancelled"
            return "[cancelled]"

        self.last_source = "llm"
        self.last_usage = getattr(client, "last_usage", None)
        self._remember(question, answer, profile)
        return answer

    def _remember(self, question: str, answer: str, profile) -> None:
        # Caller must hold _ask_lock (ask path).
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        self.history = self.history[-profile.max_history:]

    def reset(self) -> None:
        with self._ask_lock:
            self.history.clear()
