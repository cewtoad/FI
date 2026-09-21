"""DeepSeek API client - minimal, dependency-light.

Uses only the standard library (urllib) so there is nothing extra to install
for a connectivity check. Reads config from a .env file next to this module or
from environment variables.

Env / .env keys:
    DEEPSEEK_API_KEY    (required)
    DEEPSEEK_BASE_URL   (default https://api.deepseek.com)
    DEEPSEEK_MODEL      (default deepseek-chat)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

_DEFAULT_BASE = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"


def load_dotenv(path: Optional[Path] = None) -> Dict[str, str]:
    """Read a simple KEY=VALUE .env file (no external dependency)."""
    env: Dict[str, str] = {}
    p = path or (Path(__file__).parent / ".env")
    if not p.exists():
        return env
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


class DeepSeekClient:
    """Thin OpenAI-compatible chat client for DeepSeek."""

    def __init__(self, config: Optional[Dict[str, str]] = None) -> None:
        cfg = {**load_dotenv(), **os.environ}
        if config:
            cfg.update(config)
        self.api_key = cfg.get("DEEPSEEK_API_KEY", "").strip()
        self.base_url = cfg.get("DEEPSEEK_BASE_URL", _DEFAULT_BASE).rstrip("/")
        self.model = cfg.get("DEEPSEEK_MODEL", _DEFAULT_MODEL)
        self.timeout = float(cfg.get("DEEPSEEK_TIMEOUT", "30"))
        self.last_usage: Optional[Dict[str, Any]] = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and self.api_key != "sk-your-key-here"

    def chat(self, messages: List[Dict[str, str]],
             temperature: float = 0.3,
             max_tokens: int = 512) -> str:
        """Send a chat completion request and return the assistant text.

        Handles reasoning models (e.g. deepseek-flash) which emit a separate
        ``reasoning_content`` field: we return the final ``content``. If the
        content is empty because reasoning consumed the token budget, we raise
        a clear error instead of returning an empty string.

        Raises RuntimeError on any failure with a readable message.
        """
        if not self.configured:
            raise RuntimeError("DEEPSEEK_API_KEY not set (see .env)")

        url = f"{self.base_url}/chat/completions"
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Network error: {e.reason}") from e

        self.last_usage = data.get("usage")
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"Unexpected response shape: {data}") from e

        content = message.get("content") or ""
        if not content and message.get("reasoning_content"):
            raise RuntimeError(
                "Model returned only reasoning_content (token budget exhausted). "
                "Increase max_tokens or use a non-reasoning model."
            )
        return content

    def model_list(self) -> Any:
        """GET /models to verify the key works."""
        if not self.configured:
            raise RuntimeError("DEEPSEEK_API_KEY not set (see .env)")
        req = urllib.request.Request(
            f"{self.base_url}/models",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
