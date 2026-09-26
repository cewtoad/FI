"""T10a: build manifest, zip helper, and the launcher selftest wiring."""

from __future__ import annotations

import inspect
import zipfile

import build_manifest
from tools.make_zip import make_zip


def test_manifest_modules_exist_on_disk():
    # Modules that exist now must be listed; forward-declared ones (T4/T5/T6)
    # may not exist yet, so only assert the ones we ship.
    present = {p.name for p in build_manifest.ROOT.glob("*.py")}
    for core in ("state.py", "receiver.py", "app.py", "config.py", "ticker.py",
                 "speech.py", "contracts.py"):
        assert core in build_manifest.RUNTIME_MODULES, core
        assert core in present


def test_core_build_no_longer_excludes_audio():
    src = (build_manifest.ROOT / "build_release.ps1").read_text(encoding="utf-8")
    assert "--exclude-module sounddevice" not in src
    assert "--exclude-module numpy" not in src


def test_release_workflow_matches_ps1_audio_policy():
    wf = (build_manifest.ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8")
    assert "--exclude-module sounddevice" not in wf
    assert "--exclude-module numpy" not in wf


def test_both_build_paths_use_the_shared_manifest_args():
    """Core build args come from build_manifest, so CI/local cannot drift.

    The CI build previously hard-coded the arg list and silently lost
    --add-data data (data/driver_names.json)."""
    root = build_manifest.ROOT
    ps1 = (root / "build_release.ps1").read_text(encoding="utf-8")
    wf = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for src in (ps1, wf):
        assert "build_manifest.py pyinstaller-args" in src
    # data/ must be shipped in the emitted args.
    assert "--add-data" in build_manifest.pyinstaller_args()
    assert "data;data" in build_manifest.pyinstaller_args()


def test_make_zip_excludes_user_data(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "config.py").write_text("x=1", encoding="utf-8")
    (stage / ".env").write_text("SECRET=1", encoding="utf-8")
    (stage / "sessions").mkdir()
    (stage / "sessions" / "s.json").write_text("{}", encoding="utf-8")
    (stage / "__pycache__").mkdir()
    (stage / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    out = tmp_path / "out.zip"
    make_zip(str(out), str(stage))
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert any(n.endswith("config.py") for n in names)
    assert not any(n.endswith(".env") for n in names)
    assert not any("sessions" in n for n in names)
    assert not any("__pycache__" in n for n in names)


def test_fi_has_selftest_and_config_modes():
    import FI
    src = inspect.getsource(FI)
    assert "--selftest" in src
    assert "--config" in src
    assert "tool_selftest" in src


def test_start_bat_is_ascii_and_exists():
    p = build_manifest.ROOT / "start.bat"
    assert p.exists()
    p.read_bytes().decode("ascii")  # must be pure ASCII


def test_udp_port_guard_blocks_second_instance():
    """Only one process may receive the F1 UDP stream; a second must be refused."""
    import socket

    import FI

    assert FI._require_port_free(20801, force=False) is True
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", 20801))
        assert FI._require_port_free(20801, force=False) is False
        assert FI._require_port_free(20801, force=True) is True
    finally:
        s.close()


def test_fi_supports_port_and_force_flags():
    import inspect

    import FI
    src = inspect.getsource(FI)
    assert '"--port"' in src
    assert '"--force"' in src
    # forwarding guidance for the coexist case (SimHub forward -> our --port)
    assert "转发" in src and "20778" in src


def test_make_zip_drops_hf_cache_junk_keeps_model(tmp_path):
    """HF hub cache junk must not be packed; the model blobs/snapshots must be."""
    stage = tmp_path / "stage"
    model = stage / "stt_models" / "models--Systran--faster-whisper-small"
    (model / "blobs").mkdir(parents=True)
    (model / "snapshots" / "rev").mkdir(parents=True)
    (model / "refs").mkdir(parents=True)
    (model / "blobs" / "abc123").write_bytes(b"WEIGHTS")
    (model / "snapshots" / "rev" / "model.bin").write_bytes(b"WEIGHTS")
    # junk that must be dropped
    (stage / "stt_models" / ".locks").mkdir()
    (stage / "stt_models" / ".locks" / "lock").write_bytes(b"x")
    (model / "trees").mkdir()
    (model / "trees" / "t.json").write_text("{}", encoding="utf-8")
    (stage / "stt_models" / ".agent_harnesses.json").write_text("{}", encoding="utf-8")
    (stage / "stt_models" / "CACHEDIR.TAG").write_text("tag", encoding="utf-8")

    out = tmp_path / "out.zip"
    make_zip(str(out), str(stage))
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        blob = z.read(next(n for n in names if n.endswith("blobs/abc123")))
    assert blob == b"WEIGHTS"
    assert any("snapshots/rev/model.bin" in n for n in names)
    assert not any(".locks" in n for n in names)
    assert not any("/trees/" in n for n in names)
    assert not any(".agent_harnesses.json" in n for n in names)
    assert not any("CACHEDIR.TAG" in n for n in names)
