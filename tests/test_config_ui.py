"""T7: configuration page endpoints."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import config_ui


def _serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), config_ui._Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]


def _req(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn.request(method, path, body=json.dumps(body) if body is not None else None,
                 headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    conn.close()
    return resp.status, data


def test_schema_endpoint_lists_settings():
    httpd, port = _serve()
    try:
        status, body = _req(port, "GET", "/api/schema")
        assert status == 200
        data = json.loads(body)
        keys = {s["key"] for s in data["settings"]}
        assert "RADIO_VERBOSITY" in keys and "LLM_API_KEY" in keys
        # secret masked
        api = next(s for s in data["settings"] if s["key"] == "LLM_API_KEY")
        assert api["secret"] is True
    finally:
        httpd.shutdown(); httpd.server_close()


def test_settings_roundtrip(monkeypatch, tmp_path):
    import config
    cfg = config.Config(env_path=tmp_path / ".env")
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(config_ui, "get_config", lambda: cfg)
    result = config_ui.apply_settings({"RADIO_VERBOSITY": "normal"})
    assert result["applied"]["RADIO_VERBOSITY"] == "normal"
    assert cfg.get("RADIO_VERBOSITY") == "normal"


def test_settings_invalid_value_reports_error(monkeypatch, tmp_path):
    import config
    cfg = config.Config(env_path=tmp_path / ".env")
    monkeypatch.setattr(config_ui, "get_config", lambda: cfg)
    result = config_ui.apply_settings({"RADIO_VERBOSITY": "shouting"})
    assert "RADIO_VERBOSITY" in result["errors"]
    assert result["applied"] == {}


def test_post_settings_via_http(monkeypatch, tmp_path):
    import config
    cfg = config.Config(env_path=tmp_path / ".env")
    monkeypatch.setattr(config_ui, "get_config", lambda: cfg)
    httpd, port = _serve()
    try:
        status, body = _req(port, "POST", "/api/settings",
                            {"RADIO_GAP_MODE": "every_lap"})
        assert status == 200
        assert json.loads(body)["applied"]["RADIO_GAP_MODE"] == "every_lap"
        # invalid -> errors dict
        status, body = _req(port, "POST", "/api/settings",
                            {"RADIO_GAP_MODE": "nonsense"})
        assert status == 200
        assert "RADIO_GAP_MODE" in json.loads(body)["errors"]
    finally:
        httpd.shutdown(); httpd.server_close()


def test_post_rejected_from_non_local():
    httpd, port = _serve()
    # Forge a non-local client address by patching _local.
    orig = config_ui._Handler._local
    config_ui._Handler._local = lambda self: False
    try:
        status, body = _req(port, "POST", "/api/settings", {"RADIO_ENABLE": "0"})
        assert status == 403
    finally:
        config_ui._Handler._local = orig
        httpd.shutdown(); httpd.server_close()


def test_voices_endpoint():
    httpd, port = _serve()
    try:
        status, body = _req(port, "GET", "/api/voices")
        assert status == 200
        assert "voices" in json.loads(body)
    finally:
        httpd.shutdown(); httpd.server_close()
