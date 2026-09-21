"""Standalone voice runner: tap SPACE -> record -> STT -> engineer -> TTS.

Flow:
    tap SPACE  -> start recording (G733 mic)
    tap SPACE  -> stop recording (or auto-stop after MAX_RECORD_S seconds)
    transcribe (local faster-whisper)
    ask the engineer (DeepSeek)
    speak the answer (Windows SAPI on the G733 headset)

Recording/answers are driven off the live telemetry state, so it must run
alongside the receiver (same process or one that shares state). For a first
smoke test we accept a mock state; see run.py integration for the real one.

Run standalone (uses a fake static snapshot so you can test the voice loop
without the game):
    py -3.12 voice_runner.py
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Optional

from voice_stt import StreamingRecorder
from voice_trigger import RawKeyTrigger, TRIGGER_VK
from voice_tts import LocalTTS

MAX_RECORD_S = 10.0


class VoiceRunner:
    def __init__(self, snapshot_provider: Optional[Callable[[], Any]] = None,
                 ask: Optional[Callable[[str, Any], str]] = None,
                 stt_model: str = "small",
                 input_device: str = "G733",
                 output_device: str = "G733") -> None:
        self.snapshot_provider = snapshot_provider
        self._ask = ask
        self.recorder = StreamingRecorder(input_device=input_device,
                                          max_seconds=MAX_RECORD_S)
        self.tts = LocalTTS(output_device=output_device)
        self.stt = None  # lazy: LocalSTT
        self.stt_model = stt_model
        self.language = "zh"

        self._recording = False
        self._busy = False
        self._lock = threading.Lock()
        self._timeout_timer: Optional[threading.Timer] = None
        self.trigger: Optional[RawKeyTrigger] = None

    # ------------------------------------------------------------------ setup

    def _ensure_stt(self):
        if self.stt is None:
            from voice_stt import LocalSTT
            self.stt = LocalSTT(model_size=self.stt_model,
                                input_device=self.recorder.input_device,
                                language=self.language)
        return self.stt

    # ------------------------------------------------------------- key action

    def _on_tap(self) -> None:
        with self._lock:
            if self._busy:
                print("[voice] 正在处理上一个问题，忽略本次触发")
                return
            if not self._recording:
                self._start_recording()
            else:
                self._stop_and_answer()

    def _start_recording(self) -> None:
        try:
            self.recorder.start()
        except Exception as e:  # noqa: BLE001
            print(f"[voice] 录音启动失败: {e}")
            return
        self._recording = True
        # auto-stop guard
        self._timeout_timer = threading.Timer(MAX_RECORD_S + 0.5, self._auto_stop)
        self._timeout_timer.daemon = True
        self._timeout_timer.start()
        print(f"[voice] ● 录音中…（再按一次空格停止，{MAX_RECORD_S:.0f}s 自动停）")

    def _auto_stop(self) -> None:
        with self._lock:
            if self._recording:
                print("[voice] 超时，自动停止")
                self._stop_and_answer()

    def _stop_and_answer(self) -> None:
        self._recording = False
        if self._timeout_timer:
            self._timeout_timer.cancel()
            self._timeout_timer = None
        try:
            audio = self.recorder.stop()
        except Exception as e:  # noqa: BLE001
            print(f"[voice] 停止录音失败: {e}")
            return

        self._busy = True
        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _process(self, audio) -> None:
        try:
            dur = len(audio) / 16000.0 if audio is not None else 0
            if dur < 0.3:
                print("[voice] 录音太短，忽略")
                return
            print(f"[voice] 识别中…（{dur:.1f}s 音频）")
            stt = self._ensure_stt()
            t0 = time.time()
            question = stt.transcribe(audio)
            dt = time.time() - t0
            print(f"[voice] 识别耗时 {dt:.2f}s")
            if not question:
                print("[voice] 没听清")
                return
            print(f"[voice] 你说: 「{question}」")

            snapshot = self.snapshot_provider() if self.snapshot_provider else None
            if self._ask is not None and snapshot is not None:
                answer = self._ask(question, snapshot)
            else:
                answer = f"（对话模块未接入）你刚才说的是：{question}"
            print(f"[voice] AI: {answer}")
            print("[voice] 播报中…")
            self.tts.speak(answer)
        finally:
            self._busy = False
            print("[voice] 就绪，按空格提问")

    # ----------------------------------------------------------------- public

    def start(self) -> None:
        self.trigger = RawKeyTrigger(on_tap=self._on_tap, vk=TRIGGER_VK)
        # Run the Raw Input message loop on the MAIN thread: delivery to a
        # message-only window is only reliable there. Work happens on worker
        # threads spawned by _on_tap, so this loop stays responsive.
        print("语音就绪。按空格提问（再按一次停止）。Ctrl+C 退出。")
        self.trigger.run_blocking()

    def stop(self) -> None:
        if self.trigger:
            self.trigger.stop()


def main():
    runner = VoiceRunner()
    try:
        runner.start()   # blocks in the Raw Input message loop
    except KeyboardInterrupt:
        pass
    finally:
        runner.stop()
        print("\n退出。")


if __name__ == "__main__":
    sys.exit(main())
