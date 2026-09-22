"""Unit tests for the new extension points: config, llm_client, profiles."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from config import Config
from llm_client import FallbackLLM, LLMError, OpenAICompatClient, make_llm
from profiles import PROFILES, LocalRouter, get_profile


# --------------------------------------------------------------------- config

def test_config_env_and_overlay(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("LLM_MODEL=from-file\nOTHER=1\n", encoding="utf-8")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    cfg = Config(env_path=env)
    assert cfg.get("LLM_MODEL") == "from-file"
    cfg.set_runtime("LLM_MODEL", "runtime", persist=False)
    assert cfg.get("LLM_MODEL") == "runtime"


def test_config_environ_wins_over_file(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("LLM_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("LLM_MODEL", "from-env")
    cfg = Config(env_path=env)
    assert cfg.get("LLM_MODEL") == "from-env"


def test_config_persists_runtime_key(tmp_path: Path, monkeypatch) -> None:
    env = tmp_path / ".env"
    env.write_text("LLM_MODEL=old\n# keep comment\n", encoding="utf-8")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    cfg = Config(env_path=env)
    cfg.set_runtime("LLM_MODEL", "new")
    assert "LLM_MODEL=new" in env.read_text(encoding="utf-8")
    assert "# keep comment" in env.read_text(encoding="utf-8")


def test_config_rejects_non_runtime_key(tmp_path: Path) -> None:
    cfg = Config(env_path=tmp_path / ".env")
    with pytest.raises(KeyError):
        cfg.set_runtime("DEEPSEEK_API_KEY", "x")


# ------------------------------------------------------------------ llm client

def _client(tmp_path, monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return OpenAICompatClient(Config(env_path=tmp_path / ".env"))


def test_llm_prefers_llm_env(tmp_path, monkeypatch) -> None:
    c = _client(tmp_path, monkeypatch, LLM_API_KEY="k1", LLM_BASE_URL="https://x/v1",
                LLM_MODEL="m1", DEEPSEEK_API_KEY="k2", DEEPSEEK_MODEL="m2")
    assert c.api_key == "k1"
    assert c.base_url == "https://x/v1"
    assert c.model == "m1"


def test_llm_falls_back_to_deepseek_env(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    c = _client(tmp_path, monkeypatch, DEEPSEEK_API_KEY="k2", DEEPSEEK_MODEL="m2")
    assert c.api_key == "k2"
    assert c.model == "m2"


def test_make_llm_none_when_unconfigured(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert make_llm(Config(env_path=tmp_path / ".env")) is None


def test_fallback_only_on_retryable() -> None:
    class P:
        configured = True
        last_usage = {"total_tokens": 1}
        base_url = "primary"
        model = "p"

        def __init__(self, err):
            self.err = err

        def chat(self, *a, **k):
            raise self.err

    class OK:
        configured = True
        last_usage = {"total_tokens": 2}
        base_url = "fb"
        model = "f"

        def chat(self, *a, **k):
            return "fallback answer"

    # Retryable -> uses fallback.
    fb = FallbackLLM(P(LLMError("timeout", retryable=True)), OK())
    assert fb.chat([]) == "fallback answer"
    assert fb.last_fallback_from == "primary"

    # Non-retryable (4xx) -> does NOT silently switch.
    fb2 = FallbackLLM(P(LLMError("HTTP 401", retryable=False)), OK())
    with pytest.raises(LLMError):
        fb2.chat([])


# -------------------------------------------------------------------- profiles

def test_profile_lookup_and_defaults() -> None:
    assert get_profile("deep").max_tokens == 800
    assert get_profile("bogus").name == "standard"
    assert set(PROFILES) == {"fast", "standard", "deep"}


def _facts():
    return {
        "position": 3, "lap": 12, "last_lap_time": "1:22.100",
        "gap_to_front": "落后 0.800s", "gap_to_leader": "落后 5.200s",
        "fuel_surplus_laps": -0.6, "tyre_compound": "C3", "tyre_age_laps": 8,
        "tyre_temp_c": [101, 102, 99, 100],
    }


def test_local_router_answers_position() -> None:
    r = LocalRouter()
    a = r.answer("我现在P几", _facts())
    assert a is not None and "P3" in a.text


def test_local_router_fuel_deficit() -> None:
    a = LocalRouter().answer("油够不够", _facts())
    assert a is not None and "不足" in a.text


def test_local_router_miss_returns_none() -> None:
    r = LocalRouter()
    assert r.answer("帮我分析一下进站窗口策略", _facts()) is None
    assert r.misses == 1


def test_local_router_stats() -> None:
    r = LocalRouter()
    r.answer("我P几", _facts())
    r.answer("随便聊聊", _facts())
    s = r.stats()
    assert s["hits"] == 1 and s["misses"] == 1 and s["hit_rate"] == 0.5
