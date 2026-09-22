"""Deprecated shim - the client now lives in ``llm_client``.

Kept so older imports (``from ai_client import DeepSeekClient, load_dotenv``)
keep working. New code should import from ``llm_client`` / ``config``.
"""

from __future__ import annotations

from config import load_dotenv  # noqa: F401  (re-export)
from llm_client import (  # noqa: F401
    DeepSeekClient,
    FallbackLLM,
    LLMError,
    OpenAICompatClient,
    make_llm,
)

__all__ = [
    "load_dotenv",
    "DeepSeekClient",
    "OpenAICompatClient",
    "FallbackLLM",
    "LLMError",
    "make_llm",
]
