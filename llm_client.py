"""OpenAI-compatible LLM client with a pluggable factory.

Any endpoint speaking the OpenAI chat-completions protocol works unchanged:
DeepSeek, OpenAI, Moonshot, Qwen/DashScope, OpenRouter, a local Ollama/vLLM.

Config precedence (LLM_* wins, then legacy DEEPSEEK_*):
    LLM_API_KEY    / DEEPSEEK_API_KEY
    LLM_BASE_URL   / DEEPSEEK_BASE_URL   (default https://api.deepseek.com)
    LLM_MODEL      / DEEPSEEK_MODEL      (default deepseek-chat)
    LLM_TIMEOUT    / DEEPSEEK_TIMEOUT    (default 30)

``make_llm()`` builds the primary client plus an optional fallback client from
``LLM_FALLBACK_*``. Only network-level failures (timeout, connection refused,
5xx) fall through to the fallback; a 4xx (bad key, bad model) does NOT, so a
misconfigured key never silently swaps the model.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from config import Config, get_config

_DEFAULT_BASE = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"

# Errors that justify trying a fallback endpoint (transient / infrastructure),
# as opposed to a request the second endpoint would also reject.
_FALLBACK_HTTP_CODES = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """A chat request failed. ``retryable`` marks network-class failures."""

    def __init__(self, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class OpenAICompatClient:
    """Thin OpenAI-compatible chat client (stdlib urllib only)."""

    def __init__(self, cfg: Optional[Config] = None,
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 timeout: Optional[float] = None) -> None:
        self._cfg = cfg or get_config()
        self._override = {
            "base_url": base_url, "api_key": api_key,
            "model": model, "timeout": timeout,
        }
        self.last_usage: Optional[Dict[str, Any]] = None

    # Resolve lazily so a runtime hot-swap in Config is picked up per request.
    @property
    def api_key(self) -> str:
        if self._override["api_key"] is not None:
            return self._override["api_key"]
        return (self._cfg.get("LLM_API_KEY")
                or self._cfg.get("DEEPSEEK_API_KEY")).strip()

    @property
    def base_url(self) -> str:
        if self._override["base_url"] is not None:
            return self._override["base_url"]
        return (self._cfg.get("LLM_BASE_URL")
                or self._cfg.get("DEEPSEEK_BASE_URL")
                or _DEFAULT_BASE).rstrip("/")

    @property
    def model(self) -> str:
        if self._override["model"] is not None:
            return self._override["model"]
        return (self._cfg.get("LLM_MODEL")
                or self._cfg.get("DEEPSEEK_MODEL")
                or _DEFAULT_MODEL)

    @property
    def timeout(self) -> float:
        if self._override["timeout"] is not None:
            return self._override["timeout"]
        return self._cfg.get_float(
            "LLM_TIMEOUT", self._cfg.get_float("DEEPSEEK_TIMEOUT", 30.0))

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.api_key != "sk-your-key-here"

    def describe(self) -> Dict[str, str]:
        return {"base_url": self.base_url, "model": self.model}

    def chat(self, messages: List[Dict[str, str]],
             temperature: float = 0.3,
             max_tokens: int = 512) -> str:
        """Send a chat completion and return the assistant text.

        Reasoning models (e.g. deepseek-flash) emit a separate
        ``reasoning_content``; we return the final ``content``. If content is
        empty because reasoning consumed the whole token budget, raise clearly.
        """
        if not self.configured:
            raise LLMError("LLM API key not set (LLM_API_KEY / DEEPSEEK_API_KEY)")

        url = f"{self.base_url}/chat/completions"
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise LLMError(
                f"HTTP {e.code}: {detail}",
                retryable=e.code in _FALLBACK_HTTP_CODES) from e
        except urllib.error.URLError as e:
            raise LLMError(f"Network error: {e.reason}", retryable=True) from e
        except TimeoutError as e:
            raise LLMError("Request timed out", retryable=True) from e

        self.last_usage = data.get("usage")
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"Unexpected response shape: {data}") from e

        content = message.get("content") or ""
        if not content and message.get("reasoning_content"):
            raise LLMError(
                "Model returned only reasoning_content (token budget exhausted). "
                "Increase max_tokens or use a non-reasoning model.")
        return content

    def model_list(self) -> Any:
        """GET /models to verify the key works."""
        if not self.configured:
            raise LLMError("LLM API key not set")
        req = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise LLMError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise LLMError(f"Network error: {e.reason}") from e


class FallbackLLM:
    """Tries the primary client, falling back on retryable failures."""

    def __init__(self, primary: OpenAICompatClient,
                 fallback: OpenAICompatClient) -> None:
        self.primary = primary
        self.fallback = fallback
        self.last_usage: Optional[Dict[str, Any]] = None
        self.last_fallback_from: Optional[str] = None

    @property
    def configured(self) -> bool:
        return self.primary.configured or self.fallback.configured

    def describe(self) -> Dict[str, str]:
        return {
            "base_url": self.primary.base_url, "model": self.primary.model,
            "fallback_base_url": self.fallback.base_url,
            "fallback_model": self.fallback.model,
        }

    def chat(self, messages: List[Dict[str, str]],
             temperature: float = 0.3, max_tokens: int = 512) -> str:
        try:
            answer = self.primary.chat(messages, temperature, max_tokens)
            self.last_usage = self.primary.last_usage
            self.last_fallback_from = None
            return answer
        except LLMError as e:
            if not (e.retryable and self.fallback.configured):
                raise
            self.last_fallback_from = self.primary.base_url
            answer = self.fallback.chat(messages, temperature, max_tokens)
            self.last_usage = self.fallback.last_usage
            return answer

    def model_list(self) -> Any:
        return self.primary.model_list()


def make_llm(cfg: Optional[Config] = None) -> Optional[OpenAICompatClient]:
    """Build the active client from config, or None when unconfigured.

    A configured fallback endpoint wraps the primary in ``FallbackLLM``.
    """
    cfg = cfg or get_config()
    primary = OpenAICompatClient(cfg)
    if not primary.configured:
        return None

    fb_key = cfg.get("LLM_FALLBACK_API_KEY").strip()
    fb_url = cfg.get("LLM_FALLBACK_BASE_URL").strip()
    if fb_key and fb_url:
        fallback = OpenAICompatClient(
            cfg, base_url=fb_url, api_key=fb_key,
            model=cfg.get("LLM_FALLBACK_MODEL", "").strip() or primary.model)
        return FallbackLLM(primary, fallback)  # type: ignore[return-value]
    return primary


# Backwards-compatible alias so existing imports keep working.
DeepSeekClient = OpenAICompatClient
