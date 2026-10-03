"""Audio device pickers: detected-device dropdowns that save the choice.

Web feature panel: the schema-driven audio rows (AUDIO_INPUT / AUDIO_OUTPUT)
must render as <select>s filled from /api/audio (like the PTT row), pin on
change, and not be clobbered by the batch "save toggles" button.
Config page: same pickers + a re-scan button + GET /api/audio.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import config_ui
import webui


# --------------------------------------------------------------- web panel

def test_web_feature_form_audio_rows_are_dropdowns():
    p = webui.PAGE
    assert 's.key === "AUDIO_INPUT" || s.key === "AUDIO_OUTPUT"' in p
    assert "fillAudioSel" in p
    assert "audRescanBtn" in p


def test_web_audio_pin_saves_on_change_and_batch_save_skips_it():
    p = webui.PAGE
    assert "function pinAudio" in p
    # both the hand-made picker and the schema rows post to /api/audio
    assert '"f_AUDIO_INPUT","input"' in p and '"f_AUDIO_OUTPUT","output"' in p
    # the batch save must not overwrite a pin with a display-only value
    assert 'if (s.key === "AUDIO_INPUT" || s.key === "AUDIO_OUTPUT") continue;' in p


def test_web_unplugged_pin_stays_visible():
    p = webui.PAGE
    assert 't("audio.missing")' in p
    assert "fillAudioSel" in p


# ------------------------------------------------------------- config page

def test_config_page_audio_select_and_rescan():
    page = config_ui._page()
    assert "AUDIO_INPUT" in page and "AUDIO_OUTPUT" in page
    assert "loadAudio" in page and "audBtn" in page
    assert "/api/audio" in page


def test_config_ui_audio_payload_shape():
    d = config_ui._audio_payload()
    for k in ("input", "output", "current", "active"):
        assert k in d, k
    assert isinstance(d["input"], list) and isinstance(d["output"], list)


def test_config_ui_audio_endpoint_serves_json():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), config_ui._Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/audio",
                                    timeout=15) as r:
            assert r.status == 200
            d = json.loads(r.read().decode("utf-8"))
        for k in ("input", "output", "current", "active"):
            assert k in d, k
    finally:
        httpd.shutdown()
        httpd.server_close()
