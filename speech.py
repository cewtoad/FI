"""Unified speech output: AudioPlayer + SpeechArbiter (T3.4).

This is the single place anything is spoken from. It replaces the blocking
``LocalTTS._play_wav`` (which could not be interrupted) with:

  * AudioPlayer  - non-blocking playback of a WAV/MP3 payload on the configured
                   output device, with stop().
  * SpeechArbiter- a priority queue with start-of-submit synthesis, expiry,
                   an optional straight-line timing gate, interruption, and a
                   recording pause (PTT).

Only numpy + sounddevice are used (already project dependencies); the player
never blocks the playback thread on synthesis.
"""

from __future__ import annotations

import heapq
import io
import logging
import threading
import time
import wave
from typing import Callable, Dict, Optional

from contracts import (PRIORITY_ANSWER, PRIORITY_P0, PRIORITY_P1, PRIORITY_P2,
                       Utterance)

# Default expiry per priority bucket (seconds): stale info is worse than none.
DEFAULT_EXPIRIES = {PRIORITY_P0: 30.0, PRIORITY_ANSWER: 30.0,
                    PRIORITY_P1: 20.0, PRIORITY_P2: 15.0}


class UnsupportedAudioFormat(RuntimeError):
    pass


class AudioPlayer:
    """Non-blocking playback of audio bytes on the resolved output device."""

    def __init__(self, output_device: str = "",
                 logger: Optional[logging.Logger] = None) -> None:
        self.output_device = output_device
        self._log = logger or logging.getLogger("f1_tr.audio_player")
        self._playing = threading.Event()
        self.last_error: Optional[str] = None

    # -- public API -------------------------------------------------------

    def play(self, audio: bytes, mime: str) -> None:
        """Start playing ``audio`` in the background (returns immediately)."""
        if not audio:
            return
        samples, sr = self._decode(audio, mime)
        # Mark busy *before* the thread starts so is_playing() is truthful
        # immediately (the arbiter serialises clips on it).
        self._playing.set()
        threading.Thread(target=self._play_samples, args=(samples, sr),
                         daemon=True, name="f1tr-playback").start()

    def stop(self) -> None:
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:  # noqa: BLE001
            pass
        self._playing.clear()

    def is_playing(self) -> bool:
        return self._playing.is_set()

    # -- internals --------------------------------------------------------

    def _decode(self, audio: bytes, mime: str):
        mime = (mime or "").lower()
        if mime in ("audio/wav", "audio/x-wav", "audio/wave") or audio[:4] == b"RIFF":
            return self._decode_wav(audio)
        if mime in ("audio/mpeg", "audio/mp3"):
            # STOP POINT (#3): decoding MP3 needs a new dependency (audioop is
            # gone in 3.13 and banned). Raised so callers can fall back.
            raise UnsupportedAudioFormat(
                "MP3 playback needs a decoder dependency (see PLAN stop point #3)")
        raise UnsupportedAudioFormat(f"unsupported audio mime: {mime!r}")

    @staticmethod
    def _decode_wav(audio: bytes):
        import numpy as np

        with wave.open(io.BytesIO(audio), "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            sw = w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sw != 2:
            raise UnsupportedAudioFormat(f"unsupported sample width: {sw}")
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if ch > 1:
            data = data.reshape(-1, ch)
        return data, sr

    def _play_samples(self, data, sr) -> None:
        try:
            import numpy as np
            import sounddevice as sd

            import audio

            dev = audio.active_device("output", self.output_device or None)
            idx = dev["id"]
            if idx is None:
                self.last_error = "没有可用的输出设备（未连接扬声器/耳机？）"
                self._log.warning(self.last_error)
                return
            try:
                info = sd.query_devices(idx) if idx is not None else sd.query_devices(kind="output")
                dev_sr = int(info.get("default_samplerate") or sr)
                max_out = int(info.get("max_output_channels") or 2)
            except Exception:
                dev_sr, max_out = sr, 2
            if dev_sr != sr:
                data = _resample(data, sr, dev_sr)
            if data.ndim == 1 and max_out >= 2:
                data = np.column_stack([data, data])
            elif data.ndim == 2 and data.shape[1] == 1 and max_out >= 2:
                data = np.repeat(data, 2, axis=1)
            self.last_error = None
            if not self._playing.is_set():
                return  # stop()/interrupt_all() arrived before we started
            sd.play(data, dev_sr, device=idx)
            # Poll so stop() can break out (sd.wait() cannot be interrupted).
            while self._playing.is_set():
                stream = sd.get_stream()
                if stream is None or not stream.active:
                    break
                time.sleep(0.02)
        except Exception as e:  # noqa: BLE001
            self.last_error = f"playback failed: {e}"
            self._log.warning(self.last_error)
        finally:
            self._playing.clear()


def _resample(data, sr_in: int, sr_out: int):
    import numpy as np

    if sr_in == sr_out or data.size == 0:
        return data
    n_out = int(round(data.shape[0] * sr_out / sr_in))
    if n_out <= 0:
        return data
    src = np.linspace(0, data.shape[0] - 1, n_out)
    if data.ndim == 1:
        return np.interp(src, np.arange(data.shape[0]), data).astype(np.float32)
    cols = [np.interp(src, np.arange(data.shape[0]), data[:, c])
            for c in range(data.shape[1])]
    return np.column_stack(cols).astype(np.float32)


class SpeechArbiter:
    """Priority, gateable, interruptible single audio outlet."""

    def __init__(self, tts, player: AudioPlayer,
                 gate: Optional[Callable[[], bool]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 expiries: Optional[Dict[float, float]] = None,
                 fx: Optional[Callable[[bytes, str], bytes]] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.tts = tts
        self.player = player
        self._gate = gate
        self._clock = clock
        self._expiries = dict(DEFAULT_EXPIRIES)
        if expiries:
            self._expiries.update(expiries)
        self._fx = fx
        self._log = logger or logging.getLogger("f1_tr.speech")
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._heap = []            # (priority, seq, Utterance, audio|None)
        self._seq = 0
        self._recording = False
        self._epoch = 0            # bumped by interrupt_all()
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._synth_workers: list = []
        self._synth_n = 2         # parallel syntheses; P0 must not queue behind long clips
        self.dropped_expired = 0
        self.dropped_failed = 0
        self.spoken = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._synth_workers = [
            threading.Thread(target=self._synth_worker, daemon=True,
                             name=f"f1tr-tts-{i}")
            for i in range(self._synth_n)]
        for t in self._synth_workers:
            t.start()
        self._worker = threading.Thread(target=self._run, name="f1tr-arbiter",
                                        daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        self.player.stop()
        for t in self._synth_workers:
            # daemon threads: a hung synth (Piper g2pW) can never block exit
            t.join(timeout=1.5)
        self._synth_workers = []
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None

    # -- public API -------------------------------------------------------

    def submit(self, utt: Utterance) -> None:
        """Queue an utterance; a synth worker picks it up by priority."""
        if not utt.text:
            return  # nothing to say; an unsynthesisable head would block the queue
        with self._cv:
            self._seq += 1
            # [priority, seq, utt, audio, claimed]
            item = [utt.priority, self._seq, utt, None, False]
            heapq.heappush(self._heap, item)
            self._cv.notify_all()

    def play_now(self, text: str, priority: float = PRIORITY_P0) -> None:
        self.submit(Utterance(text=text, priority=priority, source="system",
                              created_at=self._clock(), gated=False))

    def set_recording(self, recording: bool) -> None:
        """Pause dequeueing while the driver is recording; resume on False."""
        with self._cv:
            self._recording = recording
            if not recording:
                self._cv.notify_all()

    def interrupt_all(self) -> None:
        """Stop playback and clear the queue (PTT press)."""
        self.player.stop()
        with self._cv:
            self._epoch += 1
            self._heap.clear()
            self._cv.notify_all()

    def is_speaking(self) -> bool:
        return self.player.is_playing()

    def stats(self) -> dict:
        with self._lock:
            queued = len(self._heap)
        return {"queued": queued, "spoken": self.spoken,
                "dropped_expired": self.dropped_expired,
                "dropped_failed": self.dropped_failed,
                "playing": self.player.is_playing()}

    # -- internals --------------------------------------------------------

    def _pick_synth_item(self):
        """Highest-priority pending (unclaimed, unsynthesised) item, or None."""
        best = None
        for cand in self._heap:
            if cand[3] is None and not cand[4]:
                if best is None or (cand[0], cand[1]) < (best[0], best[1]):
                    best = cand
        return best

    def _synth_worker(self) -> None:
        """Priority-aware synthesis: an arriving P0 is picked before older
        low-priority items instead of FIFO-queueing behind them."""
        while not self._stop.is_set():
            with self._cv:
                item = None
                while not self._stop.is_set():
                    item = self._pick_synth_item()
                    if item is not None:
                        item[4] = True
                        break
                    self._cv.wait(timeout=0.1)
                if item is None:
                    return
            self._do_synthesize(item)

    def _do_synthesize(self, item) -> None:
        _prio, _seq, utt, _audio, _claimed = item
        audio = b""   # b"" = failed/unavailable; must ALWAYS be set or the head blocks
        try:
            if self.tts is not None and getattr(self.tts, "available", False):
                audio = self.tts.synthesize(utt.text) or b""
                if self._fx is not None and audio:
                    try:
                        audio = self._fx(audio, getattr(self.tts, "mime", ""))
                    except Exception as e:  # noqa: BLE001
                        self._log.warning("radio fx failed: %r", e)
        except Exception as e:  # noqa: BLE001
            self._log.warning("TTS synthesis failed: %r", e)
            audio = b""
        finally:
            with self._cv:
                item[3] = audio
                self._cv.notify_all()

    def _expiry_for(self, priority: float) -> float:
        return self._expiries.get(priority,
                                  self._expiries.get(PRIORITY_P2, 15.0))

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while True:
                    if self._stop.is_set():
                        return
                    if self._recording:
                        self._cv.wait(timeout=0.2)
                        continue
                    if not self._heap:
                        self._cv.wait(timeout=0.2)
                        continue
                    # Peek top item; wait for its synthesis to finish.
                    item = self._heap[0]
                    if item[3] is None:
                        # Not synthesised yet. Expire it here too: expiry used
                        # to be checked only after pop, so a head that never
                        # finished synthesis blocked the queue forever.
                        head = item[2]
                        if (self._clock() - head.created_at) > self._expiry_for(head.priority):
                            heapq.heappop(self._heap)
                            self.dropped_expired += 1
                            continue
                        # Give the pool a moment; higher-priority arrivals can
                        # still jump in front meanwhile.
                        self._cv.wait(timeout=0.05)
                        continue
                    heapq.heappop(self._heap)
                    break
            try:
                if not item[3]:
                    self.dropped_failed += 1   # synthesis failed / TTS unavailable
                    continue
                utt = item[2]
                # Expiry: drop stale messages.
                if (self._clock() - utt.created_at) > self._expiry_for(utt.priority):
                    self.dropped_expired += 1
                    continue
                # Timing gate: only for gated utterances.
                if utt.gated and self._gate is not None:
                    if not self._wait_for_gate(utt):
                        self.dropped_expired += 1
                        continue
                self._emit(utt, item[3])
                # Single outlet: do not start the next clip while this one plays
                # (sd.play() would silently cut it off). interrupt_all() ->
                # player.stop() clears is_playing and releases this wait.
                while not self._stop.is_set() and self.player.is_playing():
                    time.sleep(0.02)
            except Exception as e:  # noqa: BLE001
                # One bad clip (e.g. a corrupt WAV) must never kill the only
                # playback worker: count it and keep serving the queue.
                self.dropped_failed += 1
                self._log.warning("playback pipeline error: %r", e)
                continue

    def _wait_for_gate(self, utt) -> bool:
        """Return True when the gate opens before expiry (else drop)."""
        deadline = utt.created_at + self._expiry_for(utt.priority)
        epoch = self._epoch
        while not self._stop.is_set():
            # PTT pressed (recording / interrupt_all) while we waited: this
            # popped item is no longer in the heap, so drop it explicitly or it
            # would be spoken over the driver's microphone.
            if self._recording or self._epoch != epoch:
                return False
            try:
                if self._gate():
                    return True
            except Exception:  # noqa: BLE001
                return True  # a broken gate must not mute the radio
            if self._clock() >= deadline:
                return False
            time.sleep(0.05)
        return False

    def _emit(self, utt: Utterance, audio: Optional[bytes]) -> None:
        if audio is None:
            return
        try:
            self.player.play(audio, getattr(self.tts, "mime", "audio/wav"))
            self.spoken += 1
        except Exception as e:  # noqa: BLE001
            # Unsupported/corrupt payload: text is still shown by the caller;
            # log so the queue keeps moving.
            self._log.warning("playback skipped: %s", e)


class _NullTTS:
    available = False
    mime = "audio/wav"

    def synthesize(self, text: str) -> bytes:
        raise RuntimeError("no TTS engine")
