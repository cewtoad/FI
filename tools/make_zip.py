"""Zip a staged directory with zipfile (T10a).

Replaces PowerShell ``Compress-Archive``, which is known to OOM on large trees
(the full pack is ~700MB with the whisper model). Excludes user data and
caches so an upgrade never overwrites a user's .env / sessions/.

Usage:
    py -3.12 tools/make_zip.py OUT.zip STAGE_DIR
"""

from __future__ import annotations

import os
import sys
import zipfile

# Paths (relative) never included in the archive.
EXCLUDE_DIR_NAMES = {
    "__pycache__", ".git", ".pytest_cache", "sessions",
    # HuggingFace hub cache junk around the whisper model: pure metadata,
    # locks and dry-run markers. The model itself lives in blobs/ + snapshots/
    # + refs/ and MUST be kept (snapshots/* are symlinks into blobs/, which
    # zipfile follows when writing, so the real weights get stored).
    ".locks", "trees",
}
EXCLUDE_FILE_NAMES = {".env", ".agent_harnesses.json", "CACHEDIR.TAG"}
# HuggingFace download caches can appear under stt_models - drop them.
EXCLUDE_DIR_SUFFIXES = (".cache",)


def _skip_dir(name: str) -> bool:
    if name in EXCLUDE_DIR_NAMES:
        return True
    return any(name.endswith(s) for s in EXCLUDE_DIR_SUFFIXES)


def make_zip(out_path: str, stage_dir: str) -> int:
    stage = os.path.abspath(stage_dir)
    base = os.path.dirname(stage)
    count = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for root, dirs, files in os.walk(stage):
            dirs[:] = [d for d in dirs if not _skip_dir(d)]
            for f in files:
                if f in EXCLUDE_FILE_NAMES:
                    continue
                full = os.path.join(root, f)
                arc = os.path.relpath(full, base)
                z.write(full, arc)
                count += 1
    return count


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: make_zip.py OUT.zip STAGE_DIR", file=sys.stderr)
        return 2
    n = make_zip(sys.argv[1], sys.argv[2])
    print(f"zipped {n} files -> {sys.argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
