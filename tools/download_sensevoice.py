"""Download the SenseVoice-Small sherpa-onnx model into stt_models/sensevoice.

Files: model.int8.onnx (~230 MB) + tokens.txt. Uses HF_ENDPOINT as mirror
(default https://hf-mirror.com for CN networks). Existing files are kept.

Usage:
    py -3.12 -m tools.download_sensevoice
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paths import app_root  # noqa: E402

REPO = "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
FILES = ("model.int8.onnx", "tokens.txt")


def _progress(name: str):
    last = [0.0, time.time()]

    def hook(count, block, total):
        done = count * block
        if total <= 0:
            return
        now = time.time()
        if done / total - last[0] >= 0.05 or (now - last[1] > 5 and done):
            print(f"  {name}: {done / 1e6:5.0f} / {total / 1e6:.0f} MB "
                  f"({done / total:4.0%})", flush=True)
            last[0] = done / total
            last[1] = now
    return hook


def main() -> int:
    base = (os.environ.get("HF_ENDPOINT") or "https://hf-mirror.com").rstrip("/")
    dest = app_root() / "stt_models" / "sensevoice"
    dest.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        out = dest / name
        if out.exists() and out.stat().st_size > 1024:
            print(f"已有 {out}（{out.stat().st_size / 1e6:.0f} MB），跳过")
            continue
        url = f"{base}/{REPO}/resolve/main/{name}"
        print(f"下载 {url}", flush=True)
        tmp = out.with_suffix(out.suffix + ".part")
        try:
            urllib.request.urlretrieve(url, tmp, _progress(name))
        except Exception as e:  # noqa: BLE001 - network errors are expected
            print(f"下载失败：{e!r}\n  换镜像重试：set HF_ENDPOINT=https://hf-mirror.com",
                  file=sys.stderr)
            tmp.unlink(missing_ok=True)
            return 1
        tmp.rename(out)
        print(f"完成 {out}（{out.stat().st_size / 1e6:.0f} MB）", flush=True)
    print("SenseVoice 模型就绪。语音模式会自动使用（STT_LOCAL_ENGINE=sensevoice）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
