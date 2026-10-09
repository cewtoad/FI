"""Web mode: the schema-driven feature toggles (runtime panel).

The runtime web panel exposes /api/settings (GET) and /api/settings (POST,
local-only) so users can flip feature switches (radio/voice/debrief/...)
without leaving the runtime panel; /settings is the same-port twin.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer

import config_ui
import webui


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def _req(port, method, path, body=None):
    last = None
    for _ in range(3):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            headers = {"Content-Type": "application/json"} if body is not None else {}
            conn.request(method, path,
                         body=json.dumps(body) if body is not None else None,
                         headers=headers)
            resp = conn.getresponse()
            data = resp.read().decode("utf-8")
            conn.close()
            return resp.status, data
        except (ConnectionError, OSError) as e:
            last = e
            time.sleep(0.2)
    raise last


def test_page_has_feature_toggle_panel():
    assert 'id="featBox"' in webui.PAGE
    assert "loadFeatures" in webui.PAGE
    assert 'href="/settings"' in webui.PAGE  # same-port full settings page
    assert "promptOverlay" in webui.PAGE     # custom prompt textarea


def test_settings_panels_are_separate_and_dont_auto_close():
    p = webui.PAGE
    # AI settings and feature settings are two distinct panels / entry points.
    assert 'id="setup"' in p and 'id="featBox"' in p
    assert 'id="openSet"' in p and 'id="openFeat"' in p
    assert "setupClose" in p and "featClose" in p
    # regression: the panel must not be auto-hidden by the 5s refresh.
    assert "llmKnown && !noKey" not in p


def test_get_settings_lists_toggles():
    httpd, port = _serve()
    try:
        status, body = _req(port, "GET", "/api/settings")
        assert status == 200
        keys = {s["key"] for s in json.loads(body)["settings"]}
        assert "RADIO_ENABLE" in keys and "RADIO_VERBOSITY" in keys
    finally:
        httpd.shutdown(); httpd.server_close()


def test_post_settings_applies(monkeypatch, tmp_path):
    import config
    cfg = config.Config(env_path=tmp_path / ".env")
    monkeypatch.setattr(config_ui, "get_config", lambda: cfg)
    httpd, port = _serve()
    try:
        status, body = _req(port, "POST", "/api/settings",
                            {"RADIO_VERBOSITY": "normal"})
        assert status == 200
        assert json.loads(body)["applied"]["RADIO_VERBOSITY"] == "normal"
        assert cfg.get("RADIO_VERBOSITY") == "normal"
        # invalid -> per-key error
        status, body = _req(port, "POST", "/api/settings",
                            {"RADIO_VERBOSITY": "shout"})
        assert "RADIO_VERBOSITY" in json.loads(body)["errors"]
    finally:
        httpd.shutdown(); httpd.server_close()


def test_post_settings_rejected_from_non_local(monkeypatch):
    httpd, port = _serve()
    orig = webui._Handler._local_only
    webui._Handler._local_only = lambda self: False
    try:
        status, body = _req(port, "POST", "/api/settings", {"RADIO_ENABLE": "0"})
        assert status == 403
    finally:
        webui._Handler._local_only = orig
        httpd.shutdown(); httpd.server_close()


def test_stt_settings_in_schema_and_panel():
    """STT config lives in the schema (voice group) and the AI panel has the
    preset-driven UI (provider select auto-fills URL/model)."""
    import config_schema
    keys = set(config_schema.SCHEMA_BY_KEY)
    for key in ("STT_PROVIDER", "STT_LOCAL_MODEL", "STT_LOCAL_THREADS",
                "STT_API_KEY", "STT_BASE_URL", "STT_MODEL"):
        assert key in keys, key
    by_key = config_schema.SCHEMA_BY_KEY
    assert by_key["STT_PROVIDER"].choices == ("local", "cloud", "auto", "off")
    assert by_key["STT_API_KEY"].secret is True
    # Panel wiring: preset selects + STT save through /api/settings.
    assert "AI_PRESETS" in webui.PAGE and "STT_PRESETS" in webui.PAGE
    assert "setSttPreset" in webui.PAGE and "STT_PROVIDER" in webui.PAGE


def test_get_settings_masks_stt_key(monkeypatch, tmp_path):
    import config
    cfg = config.Config(env_path=tmp_path / ".env")
    monkeypatch.setattr(config_ui, "get_config", lambda: cfg)
    cfg.set_runtime("STT_API_KEY", "sk-secret-123", persist=False)
    httpd, port = _serve()
    try:
        status, body = _req(port, "GET", "/api/settings")
        assert status == 200
        payload = {s["key"]: s for s in json.loads(body)["settings"]}
        val = str(payload["STT_API_KEY"]["value"])
        assert "sk-secret-123" not in val      # masked, never leaks over GET
    finally:
        httpd.shutdown(); httpd.server_close()


def test_settings_route_and_schema_on_webui():
    httpd, port = _serve()
    try:
        status, body = _req(port, "GET", "/settings")
        assert status == 200
        assert "F1 Race Engineer" in body
        status, body = _req(port, "GET", "/api/schema")
        assert status == 200
        data = json.loads(body)
        keys = {s["key"] for s in data["settings"]}
        assert "CUSTOM_SYSTEM_PROMPT" in keys
        assert "RADIO_ENABLE" in keys
        assert "groups" in data
    finally:
        httpd.shutdown(); httpd.server_close()

