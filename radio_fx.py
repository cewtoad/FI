"""Radio sound effects: a beep and a light band-pass + saturation (T3.6).

Everything is synthesised with numpy (no bundled audio files, no scipy). When
numpy is unavailable the functions degrade gracefully: ``make_beep`` returns
empty bytes and ``apply_filter`` returns its input unchanged, so the speech
path still works.
"""

from __future__ import annotations

from typing import Optional

try:
    import numpy as _np
except Exception:  # noqa: BLE001 - numpy is optional here
    _np = None


def has_numpy() -> bool:
    return _np is not None


def make_beep(freq_hz: float = 900.0, duration_s: float = 0.09,
              sample_rate: int = 24000, amplitude: float = 0.25) -> bytes:
    """Return a short mono int16 WAV beep, or b"" when numpy is missing."""
    if _np is None:
        return b""
    import io
    import wave

    n = int(sample_rate * duration_s)
    t = _np.arange(n, dtype=_np.float32) / sample_rate
    # A gentle attack/release avoids clicks.
    env = _np.minimum(1.0, _np.minimum(t / 0.01, (duration_s - t) / 0.01))
    env = _np.clip(env, 0.0, 1.0)
    wave_f = _np.sin(2 * _np.pi * freq_hz * t) * env * amplitude
    pcm = (_np.clip(wave_f, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def apply_filter(wav_bytes: bytes, low_hz: float = 300.0,
                 high_hz: float = 3400.0) -> bytes:
    """Band-pass a mono/stereo int16 WAV via FFT mask + light tanh (T3.6).

    Returns the input unchanged when numpy is missing or the audio cannot be
    parsed. No scipy: only an FFT mask and a gentle saturation.
    """
    if _np is None or not wav_bytes:
        return wav_bytes
    try:
        import io
        import wave

        with wave.open(io.BytesIO(wav_bytes), "rb") as w:
            sr = w.getframerate()
            ch = w.getnchannels()
            sw = w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if sw != 2:
            return wav_bytes
        data = _np.frombuffer(raw, dtype="<i2").astype(_np.float32) / 32768.0
        if ch > 1:
            data = data.reshape(-1, ch)
        spec = _np.fft.rfft(data, axis=0)
        freqs = _np.fft.rfftfreq(data.shape[0], d=1.0 / sr)
        mask = ((freqs >= low_hz) & (freqs <= high_hz)).astype(_np.float32)
        if data.ndim == 1:
            spec = spec * mask
        else:
            spec = spec * mask[:, None]
        out = _np.fft.irfft(spec, n=data.shape[0], axis=0)
        out = _np.tanh(out * 1.5) * 0.85          # light saturation
        pcm = (_np.clip(out, -1.0, 1.0) * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - never break speech over an effect
        return wav_bytes


def make_radio_audio(text_wav: bytes, beep: bool = True, filt: bool = True,
                     sample_rate: int = 24000) -> bytes:
    """Prepend the beep and optionally filter; both toggles degrade safely.

    Only makes sense for WAV payloads; for other mimes the input is returned
    unchanged (the caller then skips the pre-effect).
    """
    if not text_wav:
        return text_wav
    body = apply_filter(text_wav) if filt else text_wav
    if not beep:
        return body
    b = make_beep(sample_rate=sample_rate)
    if not b:
        return body
    return _concat_wav(b, body)


def _concat_wav(a: bytes, b: bytes) -> bytes:
    """Concatenate two mono/stereo int16 WAVs (best-effort; else return b)."""
    try:
        import io
        import wave

        with wave.open(io.BytesIO(a), "rb") as wa:
            asr, ach, asw = wa.getframerate(), wa.getnchannels(), wa.getsampwidth()
            adata = wa.readframes(wa.getnframes())
        with wave.open(io.BytesIO(b), "rb") as wb:
            bsr, bch, bsw = wb.getframerate(), wb.getnchannels(), wb.getsampwidth()
            bdata = wb.readframes(wb.getnframes())
        if (asr, ach, asw) != (bsr, bch, bsw):
            return b
        buf = io.BytesIO()
        with wave.open(buf, "wb") as out:
            out.setnchannels(ach)
            out.setsampwidth(asw)
            out.setframerate(asr)
            out.writeframes(adata + bdata)
        return buf.getvalue()
    except Exception:  # noqa: BLE001
        return b
