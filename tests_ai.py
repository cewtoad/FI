"""DeepSeek connectivity test. Run: py -3.12 tests_ai.py

Checks, in order:
  1. .env is present and key looks set
  2. GET /models succeeds (auth ok)
  3. A tiny chat completion returns text
"""

from __future__ import annotations

import sys

from ai_client import DeepSeekClient


def main():
    client = DeepSeekClient()
    print(f"base_url = {client.base_url}")
    print(f"model    = {client.model}")
    print(f"key set  = {client.configured}")
    if not client.configured:
        print("\n[!] key not configured. Edit F1_TR/.env and set DEEPSEEK_API_KEY.")
        return 2

    print("\n[1] GET /models ...")
    try:
        models = client.model_list()
        ids = [m.get("id") for m in models.get("data", [])]
        print(f"    OK, models: {ids}")
    except Exception as e:
        print(f"    FAIL: {e}")
        return 1

    print("\n[2] chat completion ...")
    try:
        reply = client.chat([
            {"role": "system", "content": "You are a concise assistant. Reply in one short sentence."},
            {"role": "user", "content": "用一句中文说明你是哪个模型。"},
        ], max_tokens=300)
        print(f"    OK, reply: {reply!r}")
        if client.last_usage:
            print(f"    usage: {client.last_usage}")
    except Exception as e:
        print(f"    FAIL: {e}")
        return 1

    print("\nAI CONNECTIVITY OK")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
