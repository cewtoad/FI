"""Test GPU transcription speed (CUDA) vs CPU."""
import sys, os, time, wave
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE / "stt_lib"))
os.environ.setdefault("HF_HOME", str(HERE / "stt_models"))

import numpy as np
from faster_whisper import WhisperModel

wav = HERE / "last_recording.wav"
with wave.open(str(wav), "rb") as w:
    sr = w.getframerate(); n = w.getnframes()
    audio = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0

print(f"音频: {len(audio)/sr:.1f}s\n")

for dev, ct in (("cuda", "float16"), ("cuda", "int8_float16"), ("cpu", "int8")):
    label = f"{dev}/{ct}"
    try:
        t0 = time.time()
        m = WhisperModel("small", device=dev, compute_type=ct,
                         download_root=str(HERE / "stt_models"))
        load = time.time() - t0
        # warm up + run
        t0 = time.time()
        segs, _ = m.transcribe(audio, language="zh", beam_size=1, vad_filter=True)
        dt = time.time() - t0
        text = "".join(s.text for s in segs).strip()
        print(f"{label}: 加载 {load:.1f}s, 转录 {dt:.2f}s -> 「{text}」")
    except Exception as e:
        print(f"{label}: 失败 - {type(e).__name__}: {str(e)[:120]}")
