"""Record from microphone and transcribe with the local faster-whisper model.

Device selection by NAME (not index) so it survives replugs/restarts.
Edit INPUT_NAME / OUTPUT_NAME below, or pass --in / --out on the command line.

Usage:
    py -3.12 test_stt_mic.py            # record 5s, transcribe
    py -3.12 test_stt_mic.py 8          # record 8s
"""

from __future__ import annotations

import os
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "stt_lib"))
os.environ.setdefault("HF_HOME", str(HERE / "stt_models"))

import numpy as np
import sounddevice as sd

from faster_whisper import WhisperModel

MODEL_SIZE = "small"
SR = 16000
CHANNELS = 1

# 用名字片段匹配设备（大小写不敏感）。改这里即可换设备。
INPUT_NAME = "G733"
OUTPUT_NAME = "G733"


def find_device(name_fragment: str, kind: str):
    """Return the index of the first device whose name contains the fragment."""
    for i, d in enumerate(sd.query_devices()):
        if name_fragment.lower() in d["name"].lower():
            if kind == "input" and d["max_input_channels"] > 0:
                return i
            if kind == "output" and d["max_output_channels"] > 0:
                return i
    return None


def record(seconds: float, device_idx) -> np.ndarray:
    print(f"录音 {seconds} 秒（设备: {sd.query_devices(device_idx)['name']}）")
    for i in (3, 2, 1):
        print(f"  {i}...")
        time.sleep(1)
    print("  ● 开始说话")
    audio = sd.rec(int(seconds * SR), samplerate=SR, channels=CHANNELS,
                   dtype="float32", device=device_idx)
    sd.wait()
    print("  ■ 结束")
    return audio.flatten()


def main():
    seconds = 5.0
    for a in sys.argv[1:]:
        try:
            seconds = float(a)
        except ValueError:
            pass

    in_idx = find_device(INPUT_NAME, "input")
    out_idx = find_device(OUTPUT_NAME, "output")

    if in_idx is None:
        print(f"找不到输入设备（匹配 '{INPUT_NAME}'）。可用设备见 _list_devices.py")
        return
    if out_idx is None:
        print(f"找不到输出设备（匹配 '{OUTPUT_NAME}'）。")
        return

    print(f"输入设备 [{in_idx}]: {sd.query_devices(in_idx)['name']}")
    print(f"输出设备 [{out_idx}]: {sd.query_devices(out_idx)['name']}")

    print("\n加载模型...")
    t0 = time.time()
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8",
                         download_root=str(HERE / "stt_models"))
    print(f"模型加载完成 ({time.time()-t0:.1f}s)\n")

    audio = record(seconds, in_idx)

    wav_path = HERE / "last_recording.wav"
    with wave.open(str(wav_path), "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((audio * 32767).astype(np.int16).tobytes())
    print(f"已保存录音: {wav_path}")

    print("识别中...")
    t0 = time.time()
    segments, info = model.transcribe(audio, language="zh", beam_size=5,
                                      vad_filter=True)
    text = "".join(seg.text for seg in segments).strip()
    print(f"\n识别结果: 「{text}」")
    print(f"耗时: {time.time()-t0:.2f}s (音频 {seconds}s)")


if __name__ == "__main__":
    main()
