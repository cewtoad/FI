"""Web mode: the proactive alert bar (T5.1).

Checks the page ships the alert bar + renderer, and that /api/state exposes the
alerts array from the radio director's alert log.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import webui


def test_page_has_alert_bar_and_renderer():
    assert 'id="alertbar"' in webui.PAGE
    assert "renderAlerts" in webui.PAGE


def test_api_state_exposes_alerts_from_log():
    from collections import deque

    class _State:
        def snapshot(self):
            return {"latest": {}, "trends": {}, "events": [],
                    "packet_errors": {}, "session": {}}

    class _Summariser:
        def summarise(self, snap):
            return {"facts": {}, "notes": [], "leaderboard": []}

    class _Receiver:
        def stats(self):
            return {"accepted": 0, "dropped_gate": 0}

    alert_log = deque(maxlen=10)
    alert_log.append({"id": "sc_deployed", "category": "p0", "priority": 0,
                      "text": "安全车出动", "created_at": 1.0})

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui._Handler)
    httpd.ctx = {
        "state": _State(), "summariser": _Summariser(), "receiver": _Receiver(),
        "engineer": type("E", (), {"describe": lambda s: {}})(),
        "voice": type("V", (), {"stt_available": False, "tts_available": False})(),
        "recorder": None, "alert_log": alert_log,
    }
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    port = httpd.server_address[1]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state",
                                    timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
        assert d["alerts"] and d["alerts"][0]["id"] == "sc_deployed"
        assert "race_model" in d
    finally:
        httpd.shutdown()
        httpd.server_close()
