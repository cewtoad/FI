"""Tests for the follow-system audio device policy (audio.active_device).

Uses a fake sounddevice module injected into sys.modules so the tests run on
machines without any audio hardware. What matters here is the *policy*:

  * unconfigured -> the system's current default device, re-queried per call;
  * the default can change between calls (unplug / switch) and is picked up;
  * a pinned fragment wins while it matches, and falls back loudly to the
    current default when it does not;
  * pinning an empty string un-pins (back to following the system).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import audio
from config import Config


class FakeSD:
    """Minimal sounddevice stand-in: device list + a mutable default."""

    def __init__(self, devices: list, default: tuple) -> None:
        self._devices = devices
        self.default = types.SimpleNamespace(device=default)

    def query_devices(self, *args, **kwargs):  # noqa: ARG002
        return self._devices


def _make_sd(default_in: int = 0, default_out: int = 1) -> FakeSD:
    devices = [
        {"name": "Stereo Mix", "max_input_channels": 2, "max_output_channels": 0},
        {"name": "Speakers", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "G733 Headset Mic", "max_input_channels": 1, "max_output_channels": 0},
        {"name": "G733 Headset Out", "max_input_channels": 0, "max_output_channels": 2},
    ]
    return FakeSD(devices, (default_in, default_out))


@pytest.fixture()
def fresh_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A Config backed by a temp .env, with no AUDIO_* in the environment."""
    for key in ("AUDIO_INPUT", "AUDIO_OUTPUT"):
        monkeypatch.delenv(key, raising=False)
    cfg = Config(env_path=tmp_path / ".env")
    monkeypatch.setattr(audio, "get_config", lambda: cfg)
    return cfg


def _install_fake_sd(monkeypatch: pytest.MonkeyPatch, sd: FakeSD) -> None:
    monkeypatch.setitem(sys.modules, "sounddevice", sd)


def test_unconfigured_follows_system_default(fresh_config, monkeypatch, capsys):
    _install_fake_sd(monkeypatch, _make_sd(default_in=2, default_out=3))
    dev = audio.active_device("input")
    assert dev == {"id": 2, "name": "G733 Headset Mic", "source": "default", "note": ""}
    out = audio.active_device("output")
    assert out["id"] == 3 and out["source"] == "default"
    assert capsys.readouterr().out == ""  # following the default is silent


def test_default_switch_is_picked_up_live(fresh_config, monkeypatch):
    """The 'monitor what is in use' property: a changed system default is
    reflected by the very next resolution, no restart, no cache."""
    sd = _make_sd(default_in=0, default_out=1)
    _install_fake_sd(monkeypatch, sd)
    assert audio.active_device("input")["name"] == "Stereo Mix"
    # User plugs a headset in and Windows makes it the default mid-session.
    sd.default.device = (2, 3)
    dev = audio.active_device("input")
    assert dev["name"] == "G733 Headset Mic" and dev["source"] == "default"


def test_pinned_fragment_wins_while_it_matches(fresh_config, monkeypatch):
    _install_fake_sd(monkeypatch, _make_sd(default_in=0, default_out=1))
    fresh_config.set_runtime("AUDIO_INPUT", "G733", persist=False)
    dev = audio.active_device("input")
    assert dev["id"] == 2 and dev["source"] == "pinned"


def test_missing_pin_falls_back_to_default_loudly(fresh_config, monkeypatch, capsys):
    _install_fake_sd(monkeypatch, _make_sd(default_in=0, default_out=1))
    fresh_config.set_runtime("AUDIO_INPUT", "Yeti", persist=False)
    dev = audio.active_device("input")
    # Falls back to the current default for this take, with a warning printed.
    assert dev["id"] == 0 and dev["source"] == "default"
    assert "Yeti" in dev["note"] and "找不到" in capsys.readouterr().out


def test_empty_pin_means_follow_system(fresh_config, monkeypatch):
    _install_fake_sd(monkeypatch, _make_sd(default_in=2, default_out=3))
    fresh_config.set_runtime("AUDIO_INPUT", "G733", persist=False)
    assert audio.active_device("input")["source"] == "pinned"
    # Un-pinning (web UI "跟随系统当前设备" option) goes back to the default.
    audio.set_device("input", "", persist=False)
    assert audio.active_device("input")["source"] == "default"


def test_no_devices_at_all(fresh_config, monkeypatch):
    _install_fake_sd(monkeypatch, FakeSD([], (-1, -1)))
    dev = audio.active_device("input")
    assert dev == {"id": None, "name": None, "source": "none", "note": ""}


def test_describe_reports_configured_and_active(fresh_config, monkeypatch):
    _install_fake_sd(monkeypatch, _make_sd(default_in=2, default_out=3))
    fresh_config.set_runtime("AUDIO_OUTPUT", "G733", persist=False)
    d = audio.describe()
    assert d["configured"] == {"input": "", "output": "G733"}
    assert d["active"]["input"]["source"] == "default"
    assert d["active"]["output"]["source"] == "pinned"


def test_cli_fragment_override_beats_config(fresh_config, monkeypatch):
    _install_fake_sd(monkeypatch, _make_sd(default_in=0, default_out=1))
    fresh_config.set_runtime("AUDIO_INPUT", "G733", persist=False)
    # An explicit fragment argument (e.g. --input) overrides the pin.
    dev = audio.active_device("input", "Stereo")
    assert dev["id"] == 0 and dev["source"] == "pinned"
