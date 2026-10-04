"""RTP timestamp → sample-index timeline for the scanner's 8 kHz audio stream.

Sample time (the position in the continuous stream) is the clock everything downstream
uses: it is immune to network jitter and identical between live capture and replay.
"""
from __future__ import annotations

import numpy as np

from .audio import SCANNER_RATE

MAX_GAP_FILL_S = 3600  # beyond this, treat a jump as a timestamp reset rather than silence


class RtpTimeline:
    """Places RTP payloads on a gap-free sample timeline.

    `place()` returns the PCM to append to the stream: zero-filled for lost packets,
    minus any part overlapping audio already emitted. Not thread-safe; callers lock.
    """

    def __init__(self) -> None:
        self._base_ts: int | None = None
        self._last_rtp: int | None = None
        self._offset = 0  # samples added across timestamp resets
        self.samples = 0  # total samples emitted so far = next sample index
        self.zero_filled = 0
        self.resets = 0

    def place(self, rtp_timestamp: int, pcm: np.ndarray) -> tuple[int, np.ndarray]:
        """Returns (start sample index, pcm to append). May return an empty array."""
        ts = rtp_timestamp & 0xFFFFFFFF
        if self._base_ts is None:
            self._base_ts = ts
        elif self._last_rtp is not None:
            delta = (ts - self._last_rtp) & 0xFFFFFFFF
            if delta > MAX_GAP_FILL_S * SCANNER_RATE:  # backwards or absurd jump
                self.resets += 1
                self._offset = self.samples
                self._base_ts = ts
        self._last_rtp = ts
        pos = self._offset + ((ts - self._base_ts) & 0xFFFFFFFF)
        start = self.samples
        if pos > self.samples:
            gap = pos - self.samples
            self.zero_filled += gap
            pcm = np.concatenate([np.zeros(gap, dtype=np.int16), pcm])
        elif pos < self.samples:
            pcm = pcm[self.samples - pos:]  # overlap; drop the already-emitted part
        self.samples += pcm.size
        return start, pcm


class AnchorClock:
    """Maps monotonic receive time ↔ sample index from (mono, sample) anchor points."""

    def __init__(self, anchors: list[tuple[float, float]] | None = None) -> None:
        self.mono: list[float] = []
        self.sample: list[float] = []
        for m, s in anchors or []:
            self.add(m, s)

    def add(self, mono: float, sample: float) -> None:
        self.mono.append(mono)
        self.sample.append(sample)

    def __bool__(self) -> bool:
        return bool(self.mono)

    def to_sample(self, mono: float) -> float:
        i = int(np.clip(np.searchsorted(self.mono, mono, side="right") - 1, 0, len(self.mono) - 1))
        return self.sample[i] + (mono - self.mono[i]) * SCANNER_RATE

    def to_mono(self, sample: float) -> float:
        i = int(np.clip(np.searchsorted(self.sample, sample, side="right") - 1, 0, len(self.sample) - 1))
        return self.mono[i] + (sample - self.sample[i]) / SCANNER_RATE
