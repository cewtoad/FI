"""Tests for the packaging/launcher layer: paths, version, port preflight."""

from __future__ import annotations

import socket

import paths


def test_app_root_is_project_dir_in_source():
    # From source, app_root() == this file's directory.
    assert paths.app_root() == paths.Path(__file__).resolve().parent


def test_app_root_honours_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("F1TR_ROOT", str(tmp_path))
    assert paths.app_root() == tmp_path.resolve()


def test_is_frozen_false_in_source():
    assert paths.is_frozen() is False


def test_resource_root_matches_app_root_in_source():
    assert paths.resource_root() == paths.app_root()


def test_config_env_path_under_app_root():
    from config import _ENV_PATH
    assert _ENV_PATH.parent == paths.app_root()


def test_recorder_session_dir_under_app_root():
    from recorder import SESSION_DIR
    assert SESSION_DIR.parent == paths.app_root()


def test_bump_version_parsing():
    from webui import _version_tuple
    assert _version_tuple("0.2.0") == (0, 2, 0)
    assert _version_tuple("1.10.3") > _version_tuple("1.9.9")


def test_port_preflight_detects_busy_port():
    import FI

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", 0))
        busy_port = s.getsockname()[1]
        assert FI._port_busy(busy_port) is True
    finally:
        s.close()
    # A freshly-freed port should read as free.
    assert FI._port_busy(busy_port) is False
