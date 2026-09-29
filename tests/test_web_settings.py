"""Web mode: the schema-driven feature toggles (runtime panel).

The runtime web panel exposes /api/settings (GET) and /api/settings (POST,
local-only) so users can flip feature switches (radio/voice/debrief/...)
without opening the separate config page.
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
    assert "8766" in webui.PAGE          # link to the full config page


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
