"""Unified voice entry: telemetry receiver + push-to-talk voice Q&A in one process.

Model:
    main thread   -> Raw Input message loop (NUM0 tap starts/stops recording)
    worker thread -> asyncio loop running the UDP receiver, keeping TelemetryState
    on tap        -> record -> STT -> Engineer.ask(latest snapshot) -> TTS

Run:
    py -3.12 voice_main.py
    py -3.12 voice_main.py --port 20777 --input G733 --output G733
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import time
from typing import Optional

import audio
from app import build_app
from engineer import Engineer
from receiver import DEFAULT_PORT, PACKETS_CONSUMED, TelemetryReceiver
from state import TelemetryState
from stt_client import make_stt
from voice_stt import StreamingRecorder
from voice_trigger import RawKeyTrigger, TRIGGER_VK
from voice_tts import LocalTTS

MAX_RECORD_S = 10.0


class VoiceApp:
    def __init__(self, port: int, bind_ip: str, input_dev: str, output_dev: str,
                 logger: logging.Logger) -> None:
        self.logger = logger
        # T3.7: assemble through the single composition root, so voice mode
        # shares the receiver/state/engineer/recorder wiring (and now records
        # sessions like the web path). mode="voice" also builds a
        # SpeechArbiter, which the radio director / answers route through.
        self.app = build_app(port=port, bind_ip=bind_ip, logger=logger,
                             mode="voice", audio_output=output_dev)
        self.state = self.app.state
        self.receiver = self.app.receiver
        self.engineer = self.app.engineer
        self.arbiter = self.app.speech

        # Voice side
        self.recorder = StreamingRecorder(input_device=input_dev,
                                          max_seconds=MAX_RECORD_S)
        self.tts = LocalTTS(output_device=output_dev)
        self.input_dev = input_dev or audio.current()["input"]
        # Load the STT model ONCE (loading takes ~20s; never do it per-answer).
        self._stt = None
        self._stt_lock = threading.Lock()

        self._recording = False
        self._busy = False
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self.trigger: Optional[RawKeyTrigger] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # T6.2: PTT state machine driven by config (hold/toggle + double-tap).
        from config import get_config
        from ptt_controller import PTTController
        cfg = get_config()
        self.ptt = PTTController(
            mode=cfg.get("PTT_MODE", "toggle"),
            double_tap_window_ms=cfg.get_int("PTT_DOUBLE_TAP_WINDOW_MS", 400))

    # -------------------------------------------------------------- telemetry

    def _run_receiver(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self.receiver.run())
        except Exception as e:  # noqa: BLE001
            self.logger.error("receiver stopped: %r", e)

    # ------------------------------------------------------------ key action

    def _on_ptt_press(self) -> None:
        action = self.ptt.on_press()
        self._apply_ptt_action(action)

    def _on_ptt_release(self) -> None:
        action = self.ptt.on_release()
        self._apply_ptt_action(action)

    def _apply_ptt_action(self, action) -> None:
        if not action:
            return
        if action.kind == "start_recording":
            with self._lock:
                if self._busy:
                    print("[voice] 正在处理上一个问题…", flush=True)
                    return
                if not self._recording:
                    self._start_recording()
        elif action.kind == "stop_recording":
            with self._lock:
                if self._recording:
                    self._stop_and_answer_locked()
        elif action.kind == "toggle_quiet":
            self._toggle_quiet()

    def _toggle_quiet(self) -> None:
        radio = getattr(self.app, "radio", None)
        if radio is None:
            return
        applied, msg = radio.set_quiet(not radio.quiet_state())
        from radio_templates import render as _render
        text = _render(msg)
        if not applied:
            text = _render("quiet_locked")
        if text:
            print(f"[voice] {text}", flush=True)
        # Speak the confirmation via the arbiter (system, bypasses gate).
        if self.arbiter is not None:
            self.arbiter.play_now(text)

    def _on_tap(self) -> None:
        # Kept for callers that only have a tap callback; the controller's
        # press/release path is preferred (see RawKeyTrigger on_press/on_release).
        with self._lock:
            if self._busy:
                print("[voice] 正在处理上一个问题…", flush=True)
                return
            if not self._recording:
                self._start_recording()
            else:
                self._stop_and_answer_locked()

    def _start_recording(self) -> None:
        # PTT interrupts any ongoing speech (decided in §1 / T6).
        if self.arbiter is not None:
            self.arbiter.interrupt_all()
            self.arbiter.set_recording(True)
        try:
            self.recorder.start()
        except Exception as e:  # noqa: BLE001
            print(f"[voice] 录音启动失败: {e}", flush=True)
            if self.arbiter is not None:
                self.arbiter.set_recording(False)
            return
        self._recording = True
        self._timer = threading.Timer(MAX_RECORD_S + 0.5, self._auto_stop)
        self._timer.daemon = True
        self._timer.start()
        print(f"[voice] ● 录音中…（再按小键盘+停止，{MAX_RECORD_S:.0f}s 自动停）",
              flush=True)

    def _auto_stop(self) -> None:
        with self._lock:
            if self._recording:
                print("[voice] 超时，自动停止", flush=True)
                self._stop_and_answer_locked()

    def _stop_and_answer_locked(self) -> None:
        """Stop the recorder and dispatch processing. Caller holds ``_lock``."""
        self._recording = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        try:
            samples = self.recorder.collected_samples()
        except Exception:
            samples = 0
        if samples < StreamingRecorder.MIN_SAMPLES:
            # ISSUE-1: a stop that lands before the first audio callback (or a
            # genuine tap-on/tap-off) yields an empty buffer. Report it clearly
            # instead of sending silence to the STT path.
            try:
                self.recorder.stop()
            except Exception:
                pass
            if self.arbiter is not None:
                self.arbiter.set_recording(False)
            print("[voice] 录音太短（没收到音频），已忽略", flush=True)
            return
        try:
            pcm = self.recorder.stop()
        except Exception as e:  # noqa: BLE001
            if self.arbiter is not None:
                self.arbiter.set_recording(False)
            print(f"[voice] 停止录音失败: {e}", flush=True)
            return
        # Recording is done: let the radio resume while STT/AI runs.
        if self.arbiter is not None:
            self.arbiter.set_recording(False)
        self._busy = True
        threading.Thread(target=self._process, args=(pcm,), daemon=True).start()

    def _process(self, pcm) -> None:
        try:
            dur = len(pcm) / 16000.0 if pcm is not None else 0
            if dur < 0.3:
                print("[voice] 录音太短，忽略", flush=True)
                return

            # STT (reuse a single loaded model; config-driven so .env's
            # STT_LOCAL_MODEL / STT_LOCAL_THREADS apply).
            stt = self._get_stt()
            if stt is None:
                print("[voice] STT 不可用（未安装 faster-whisper）", flush=True)
                return
            print(f"[voice] 识别中…（{dur:.1f}s）", flush=True)
            t0 = time.time()
            try:
                question = stt.transcribe(pcm)
            except Exception as e:  # noqa: BLE001 - keep the voice loop alive
                print(f"[voice] 识别失败: {e}", flush=True)
                return
            print(f"[voice] 识别耗时 {time.time()-t0:.2f}s", flush=True)
            if not question:
                print("[voice] 没听清", flush=True)
                return
            print(f"[voice] 你说: 「{question}」", flush=True)

            # Engineer with the live snapshot. T1.1: always go through ask()
            # so the local fast path (position/lap/tyre/...) works without a
            # key; ask() itself handles the unconfigured case.
            snap = self.state.snapshot()
            t1 = time.time()
            answer = self.engineer.ask(question, snap, channel="voice")
            ai_dt = time.time() - t1
            usage = self.engineer.last_usage or {}
            print(f"[voice] AI 推理耗时 {ai_dt:.2f}s "
                  f"(来源={self.engineer.last_source}, "
                  f"tokens={usage.get('total_tokens')})", flush=True)
            print(f"[voice] AI: {answer}", flush=True)

            # TTS: route through the single audio outlet (SpeechArbiter) so a
            # driver answer interrupts any proactive radio and bypasses the
            # straight-line gate. Falls back to the local shim if unavailable.
            t2 = time.time()
            print("[voice] 播报中…", flush=True)
            if self.arbiter is not None:
                from contracts import PRIORITY_ANSWER, Utterance
                self.arbiter.interrupt_all()
                self.arbiter.submit(Utterance(
                    text=answer, priority=PRIORITY_ANSWER, source="answer",
                    created_at=time.monotonic(), gated=False))
                while self.arbiter.is_speaking():
                    time.sleep(0.05)
            else:
                self.tts.speak(answer)
            print(f"[voice] TTS 耗时 {time.time()-t2:.2f}s", flush=True)
        finally:
            self._busy = False
            print("[voice] 就绪，按小键盘+提问", flush=True)

    def _get_stt(self):
        """Return the shared STT engine, building it once under a lock.

        The preloader and the first question previously both called
        ``LocalSTT(...)`` and could load two models; the lock serialises them.
        """
        with self._stt_lock:
            if self._stt is None:
                self._stt = make_stt(input_device=self.input_dev)
            return self._stt

    # ----------------------------------------------------------------- public

    def run(self) -> None:
        t = threading.Thread(target=self._run_receiver, daemon=True)
        t.start()
        print(f"遥测接收已启动 (UDP {self.receiver.port})")
        # T3.7/T5: start the ticker (race model + radio director) and the
        # speech arbiter built by build_app().
        try:
            self.app.start_background()
        except Exception as e:  # noqa: BLE001
            self.logger.warning("background pipeline failed to start: %r", e)

        # Show which devices will be used (resolved live per take from here
        # on, so this is just the status at startup).
        for kind, label in (("input", "麦克风"), ("output", "播报")):
            try:
                dev = audio.active_device(kind)
            except Exception as e:  # noqa: BLE001 - audio stack may be absent
                print(f"{label}: 未检测到（{e}）")
                continue
            if dev["id"] is None:
                print(f"{label}: 未检测到可用设备（语音功能需要）")
            else:
                tag = "已指定" if dev["source"] == "pinned" else "跟随系统当前设备"
                print(f"{label}: {dev['name']}（{tag}）")

        from paths import app_root
        env_path = app_root() / ".env"
        if self.engineer.configured:
            print("AI 已就绪")
        else:
            print("未配置 AI key：AI 问答不可用（本地快答仍可用：名次/油量/胎温…）")
            print(f"   → 填 key：编辑 {env_path}")
            print("     或改用【网页模式】（页面顶部有设置面板，可可视化填写）")

        # Preload the STT model in the background so the first question isn't
        # stuck on a ~20s model load.
        def _preload():
            stt = self._get_stt()
            if stt is None:
                print("本地语音不可用：未安装 faster-whisper。")
                print("   → 请使用【全量语音包】（内含本地语音依赖），")
                print("     或在 .env 设 STT_PROVIDER=cloud 用云端识别。")
                return
            print("STT 模型加载中…（首次约 20 秒，之后提问即时）", flush=True)
            if hasattr(stt, "load"):
                stt.load()
            print("STT 就绪。", flush=True)
        threading.Thread(target=_preload, daemon=True).start()

        # T6.1: resolve the PTT binding (kb:<vk> from config, default NUMPAD +).
        from config import get_config
        from input_sources import parse_binding
        binding = parse_binding(get_config().get("PTT_BINDING", "kb:0x6B"))
        vk = binding["vk"] if binding and binding["type"] == "kb" else TRIGGER_VK
        self.trigger = RawKeyTrigger(on_tap=self._on_tap, vk=vk,
                                     on_press=self._on_ptt_press,
                                     on_release=self._on_ptt_release)
        mode = self.ptt.mode
        print(f"PTT 模式={mode}，按键 VK=0x{vk:02X}。按 {mode} 方式说话，Ctrl+C 退出。\n")
        self.trigger.run_blocking()  # blocks (main thread)


def main() -> None:
    p = argparse.ArgumentParser(description="F1 voice race engineer")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--bind-ip", default="127.0.0.1")
    p.add_argument("--input", default="", help="麦克风设备名片段（默认取 .env AUDIO_INPUT）")
    p.add_argument("--output", default="", help="输出设备名片段（默认取 .env AUDIO_OUTPUT）")
    p.add_argument("--list-audio", action="store_true",
                   help="列出可用音频设备后退出")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    if args.list_audio:
        for kind, label in (("input", "输入(麦克风)"), ("output", "输出(播放)")):
            print(f"\n{label}:")
            for d in audio.list_devices(kind):
                mark = " (默认)" if d["default"] else ""
                print(f"  [{d['id']}] {d['name']} ({d['channels']}ch){mark}")
        return

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    logger = logging.getLogger("f1_tr.voice")

    app = VoiceApp(args.port, args.bind_ip, args.input, args.output, logger)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        if app.trigger:
            app.trigger.stop()
        try:
            app.app.shutdown()
        except Exception:
            pass
        print("\n退出。")


if __name__ == "__main__":
    main()
