"""Synthetic scanner sessions for tests: audio spans + status events on one timeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from scanner_transcriber.models import AudioFrame, ScannerStatus

RATE = 8000
FRAME = 320  # 40 ms, like the SDS200's RTP packets
T0 = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


@dataclass
class Sim:
    seconds: float
    speech: list[tuple[float, float, int]] = field(default_factory=list)  # (start, end, amp)
    statuses: list[tuple[float, dict]] = field(default_factory=list)

    def talk(self, start: float, end: float, amp: int = 1000) -> Sim:
        self.speech.append((start, end, amp))
        return self

    def status(self, t: float, mute: str, tgid: str | None = None, **kw) -> Sim:
        self.statuses.append((t, {"mute": mute, "tgid": tgid, "system": "Sys",
                                  "department": f"Dept{tgid}", "channel": f"Ch{tgid}", **kw}))
        return self

    def hold(self, start: float, end: float, mute: str, tgid: str | None = None,
             every: float = 0.25, **kw) -> Sim:
        """Repeat a status like the scanner's PSI push (~4/s) over [start, end)."""
        t = start
        while t < end - 1e-9:
            self.status(round(t, 3), mute, tgid, **kw)
            t += every
        return self

    def ptt(self, start: float, end: float, tgid: str, unit: str | None = None,
            amp: int = 1000, lag: float = 0.1) -> Sim:
        """A transmission: status Unmute slightly after speech starts, Mute after it ends."""
        self.talk(start, end, amp)
        self.status(start + lag, "Unmute", tgid, unit_id=unit)
        self.status(end + lag, "Mute", tgid)
        return self

    def audio(self) -> np.ndarray:
        pcm = np.zeros(int(self.seconds * RATE), dtype=np.int16)
        for a, b, amp in self.speech:
            n = int(b * RATE) - int(a * RATE)
            pcm[int(a * RATE):int(b * RATE)] = (amp * np.sin(np.arange(n) * 0.3)).astype(np.int16) | 1
        return pcm

    def events(self, statuses_have_sample: bool = True):
        """Frames and statuses merged in timeline order (status first on ties)."""
        pcm = self.audio()
        evs = []
        for i in range(0, len(pcm), FRAME):
            evs.append((i + FRAME, 1, AudioFrame(i, pcm[i:i + FRAME],
                                                  T0 + timedelta(seconds=i / RATE))))
        for t, fields in self.statuses:
            s = t * RATE
            st = ScannerStatus.from_summary(fields, mono=1000 + t, wall=T0 + timedelta(seconds=t),
                                            sample=s if statuses_have_sample else None)
            evs.append((s, 0, st))
        evs.sort(key=lambda e: (e[0], e[1]))
        return [e[2] for e in evs]


def run(seg, sim: Sim, flush: bool = True, **kw):
    clips = []
    for ev in sim.events(**kw):
        clips += seg.feed_audio(ev) if isinstance(ev, AudioFrame) else seg.feed_status(ev)
    if flush:
        clips += seg.flush()
    return clips
