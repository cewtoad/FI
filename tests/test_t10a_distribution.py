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
