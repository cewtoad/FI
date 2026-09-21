"""Benchmark: how long does transcription take, and does beam_size matter?"""
import sys, os, time, wave
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "stt_lib"))
os.environ.setdefault("HF_HOME", str(HERE / "stt_models"))

import numpy as np
from faster_whisper import WhisperModel

wav = HERE / "last_recording.wav"
if not wav.exists():
    print("没有 last_recording.wav，请先用 voice_runner 录一句")
    sys.exit(1)

with wave.open(str(wav), "rb") as w:
    sr = w.getframerate(); n = w.getnframes()
    audio = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
dur = len(audio) / sr
print(f"音频时长: {dur:.1f}s @ {sr}Hz")

print("加载模型...")
t0 = time.time()
model = WhisperModel("small", device="cpu", compute_type="int8",
                     download_root=str(HERE / "stt_models"))
print(f"加载: {time.time()-t0:.2f}s\n")

for beam in (5, 1):
    for vad in (True, False):
        t0 = time.time()
        segs, _ = model.transcribe(audio, language="zh", beam_size=beam, vad_filter=vad)
        text = "".join(s.text for s in segs).strip()
        dt = time.time() - t0
        print(f"beam={beam} vad={vad}: {dt:.2f}s  ->  「{text}」")

# int8 vs float32 on CPU
print("\n试试 compute_type=float32:")
m2 = WhisperModel("small", device="cpu", compute_type="float32",
                  download_root=str(HERE / "stt_models"))
t0 = time.time()
segs, _ = m2.transcribe(audio, language="zh", beam_size=1, vad_filter=True)
print(f"  float32 beam=1: {time.time()-t0:.2f}s -> 「{''.join(s.text for s in segs).strip()}」")
