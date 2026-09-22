"""Download a faster-whisper model into the project folder.

Keeps the model OUT of the C: drive (which is nearly full) by downloading into
./stt_models instead of the default HuggingFace cache.

Usage:
    py -3.12 download_stt_model.py            # default: small
    py -3.12 download_stt_model.py base       # smaller/faster, less accurate
    py -3.12 download_stt_model.py small
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make faster-whisper importable from stt_lib/
from paths import app_root

HERE = app_root()
sys.path.insert(0, str(HERE / "stt_lib"))

# Force model downloads into the project folder, not C:.
MODELS_DIR = HERE / "stt_models"
MODELS_DIR.mkdir(exist_ok=True)
os.environ.setdefault("HF_HOME", str(MODELS_DIR))


def main():
    size = sys.argv[1] if len(sys.argv) > 1 else "small"
    print(f"下载 faster-whisper 模型: {size}")
    print(f"目标目录: {MODELS_DIR}")
    print("（首次下载可能需要几分钟，模型来源 HuggingFace）\n")

    from faster_whisper import WhisperModel

    # download_root 明确指向项目目录
    model = WhisperModel(
        size,
        device="cpu",
        compute_type="int8",
        download_root=str(MODELS_DIR),
    )
    print(f"\n✓ 模型已下载并加载成功: {size}")

    # 报告占用的空间
    total = sum(f.stat().st_size for f in MODELS_DIR.rglob("*") if f.is_file())
    print(f"stt_models 当前占用: {total/1024/1024:.1f} MB")


if __name__ == "__main__":
    main()
