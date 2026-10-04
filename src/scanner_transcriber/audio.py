"""Audio helpers: G.711 µ-law decode, resampling, WAV I/O."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import soxr

SCANNER_RATE = 8000
STT_RATE = 16000


def _build_ulaw_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return np.where(sign != 0, -magnitude, magnitude).astype(np.int16)


ULAW_TABLE = _build_ulaw_table()


def ulaw_to_pcm16(data: bytes) -> np.ndarray:
    return ULAW_TABLE[np.frombuffer(data, dtype=np.uint8)]


def resample(pcm: np.ndarray, src_rate: int = SCANNER_RATE, dst_rate: int = STT_RATE) -> np.ndarray:
    """Resample int16 or float PCM; returns float32 in [-1, 1]."""
    x = pcm.astype(np.float32)
    if pcm.dtype == np.int16:
        x /= 32768.0
    if src_rate == dst_rate:
        return x
    return soxr.resample(x, src_rate, dst_rate, quality="HQ").astype(np.float32)


def write_wav(path: str | Path, pcm16: np.ndarray, rate: int = SCANNER_RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16.astype("<i2").tobytes())


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError(f"{path}: expected mono 16-bit WAV")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"), w.getframerate()


def rms_dbfs(pcm16: np.ndarray) -> float:
    if pcm16.size == 0:
        return -120.0
    rms = np.sqrt(np.mean((pcm16.astype(np.float64) / 32768.0) ** 2))
    return float(20 * np.log10(max(rms, 1e-6)))


# ---- playback levelling ----------------------------------------------------------------------
# Scanner clips are quiet (median about -30 dBFS RMS, peaks never above about -6 dBFS) and vary
# about 13 dB between talkgroups and units. For listening (not transcription, and not the
# stored file), bring each clip to a standard level, with a soft limiter so peaks can't clip.

LEVEL_TARGET_DBFS = -18.0   # speech RMS, close to typical streaming loudness
LEVEL_MAX_GAIN_DB = 18.0    # don't turn faint noise into a roar
LEVEL_MIN_GAIN_DB = -6.0
LIMIT_CEILING = 10 ** (-1 / 20)   # -1 dBFS
LIMIT_KNEE = 0.6                  # limiting starts at 60% of full scale


def level(pcm16: np.ndarray, target_dbfs: float = LEVEL_TARGET_DBFS) -> np.ndarray:
    """Return a levelled copy: gain toward `target_dbfs` (measured on non-zero samples, so the
    exact-zero padding doesn't count), then a soft limiter (slope 1 below the knee, smoothly
    approaching -1 dBFS above it)."""
    x = pcm16.astype(np.float64) / 32768.0
    voiced = x[x != 0]
    if voiced.size == 0:
        return pcm16.copy()
    rms = np.sqrt(np.mean(voiced ** 2))
    gain_db = target_dbfs - 20 * np.log10(max(rms, 1e-6))
    gain_db = min(LEVEL_MAX_GAIN_DB, max(LEVEL_MIN_GAIN_DB, gain_db))
    y = x * 10 ** (gain_db / 20)
    t, c = LIMIT_KNEE, LIMIT_CEILING
    over = np.abs(y) > t
    y[over] = np.sign(y[over]) * (t + (c - t) * np.tanh((np.abs(y[over]) - t) / (c - t)))
    return np.round(y * 32767).astype(np.int16)


def wav_bytes(pcm16: np.ndarray, rate: int = SCANNER_RATE) -> bytes:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm16.astype("<i2").tobytes())
    return buf.getvalue()
