"""Turns the continuous audio stream + scanner status into one clip per transmission (PTT).

Pure and synchronous: feed it frames and statuses in arrival order and it returns finished
clips. The audio timeline is the only clock; a status takes effect once the audio has
reached its sample position (+ audio_latency_ms).

Boundaries come from status: a clip is one continuous `Unmute` span on one talkgroup, with
a short debounce so PSI jitter doesn't split a PTT. Audio only trims the exact-zero
silence at either end (the SDS200 streams digital zeros while muted) and, while status is
stale, stands in for it: a run of non-zero audio becomes a clip.
"""
from __future__ import annotations

from collections import deque
from datetime import timedelta
from enum import Enum

import numpy as np

from .audio import SCANNER_RATE
from .config import SegmenterConfig
from .models import AudioFrame, Clip, ScannerStatus

_TRACKED = ("tgid", "channel", "department", "system", "unit_id")


class State(Enum):
    IDLE = "idle"
    OPEN = "open"
    CLOSING = "closing"   # muted, waiting out the debounce


def _ms(ms: int) -> int:
    return ms * SCANNER_RATE // 1000


class Segmenter:
    def __init__(self, cfg: SegmenterConfig | None = None) -> None:
        self.cfg = cfg = cfg or SegmenterConfig()
        self._debounce = _ms(cfg.debounce_ms)
        self._preroll = _ms(cfg.preroll_ms)
        self._pad = _ms(cfg.pad_ms)
        self._min = _ms(cfg.min_clip_ms)
        self._max = cfg.max_clip_s * SCANNER_RATE
        self._latency = _ms(cfg.audio_latency_ms)
        self._stale = _ms(cfg.status_stale_ms)
        self._unit_gap = _ms(cfg.unit_gap_ms)

        self.state = State.IDLE
        self.now = 0                      # end of the latest audio fed (sample index)
        self.status: ScannerStatus | None = None
        self._status_at: int | None = None  # sample position of the latest applied status
        self._pending: deque[ScannerStatus] = deque()
        self._ring: deque[AudioFrame] = deque()  # pre-roll while idle
        # open clip
        self._frames: list[AudioFrame] = []
        self._opened_by: ScannerStatus | None = None
        self._units: list[str] = []
        self._changes: list[dict] = []
        self._continued = False
        self._audio_mode = False          # current clip was opened without fresh status
        self._closing_since = 0
        self._last_nonzero = 0            # sample index just after the last non-zero sample

    # ---- inputs -------------------------------------------------------------

    def feed_status(self, st: ScannerStatus) -> list[Clip]:
        self._pending.append(st)
        return self._apply_due()

    def feed_audio(self, frame: AudioFrame) -> list[Clip]:
        out: list[Clip] = []
        if frame.sample > self.now and self.now and self.state is not State.IDLE:
            # timeline gap (e.g. reconnect): pad with silence so positions stay consistent
            gap = frame.sample - self.now
            self._append(AudioFrame(self.now, np.zeros(gap, np.int16),
                                    frame.wall - timedelta(seconds=gap / SCANNER_RATE)))
        self.now = frame.end
        nz = np.flatnonzero(frame.pcm)
        if nz.size:
            self._last_nonzero = frame.sample + int(nz[-1]) + 1

        if self.state is State.IDLE:
            self._ring.append(frame)
            while self._ring and self._ring[-1].end - self._ring[0].end >= self._preroll:
                self._ring.popleft()
        else:
            self._append(frame)

        out += self._apply_due()
        out += self._tick(frame, has_audio=bool(nz.size))
        return out

    def flush(self) -> list[Clip]:
        """Emit any open clip (shutdown)."""
        if self.state is State.IDLE:
            return []
        clip = self._emit()
        return [clip] if clip else []

    # ---- internals ----------------------------------------------------------

    def _fresh(self) -> bool:
        return self._status_at is not None and self.now - self._status_at <= self._stale

    def _apply_due(self) -> list[Clip]:
        out: list[Clip] = []
        while self._pending:
            st = self._pending[0]
            due = self.now if st.sample is None else st.sample + self._latency
            if due > self.now:
                break
            self._pending.popleft()
            out += self._on_status(st, at=max(int(due), 0))
        return out

    def _on_status(self, st: ScannerStatus, at: int) -> list[Clip]:
        out: list[Clip] = []
        self.status, self._status_at = st, self.now
        if self.state is State.IDLE:
            if st.unmuted:
                self._open(st, preroll=True)
            return out

        opened_tg = self._opened_by.tgid if self._opened_by else None
        tg_changed = bool(st.tgid and opened_tg and st.tgid != opened_tg)
        if st.unmuted and tg_changed:
            # scanner moved to another talkgroup without a Mute in between
            out += self._emit_list()
            self._open(st, preroll=False)
            return out
        if self._audio_mode and st.unmuted and not opened_tg:
            self._opened_by, self._audio_mode = st, False  # status caught up; adopt its label
        if (self.cfg.split_on_unit_change and st.unmuted and st.unit_id and self._units
                and st.unit_id not in self._units):
            # The scanner may stay unmuted from one unit's PTT straight into another's reply.
            clip = self._split_at_handoff(st)
            if clip is not None:
                return out + [clip]
        self._track(st)
        if self.state is State.OPEN and not st.unmuted:
            self.state, self._closing_since = State.CLOSING, at
        elif self.state is State.CLOSING and st.unmuted:
            self.state = State.OPEN
        return out

    def _tick(self, frame: AudioFrame, has_audio: bool) -> list[Clip]:
        if not self._fresh():
            return self._tick_audio_mode(frame, has_audio)
        if self.state is State.CLOSING and self.now - self._closing_since >= self._debounce:
            return self._emit_list()
        if self.state is State.OPEN and self.now - self._clip_start() >= self._max:
            return self._split_continued()
        return []

    def _tick_audio_mode(self, frame: AudioFrame, has_audio: bool) -> list[Clip]:
        """Status is stale: a clip is a run of non-zero audio, closed by `debounce` of zeros."""
        if self.state is State.IDLE:
            if has_audio:
                self._open(self.status, preroll=True, audio_mode=True)
            return []
        if self.now - self._last_nonzero >= self._debounce:
            return self._emit_list()
        if self.now - self._clip_start() >= self._max:
            return self._split_continued()
        return []

    def _open(self, st: ScannerStatus | None, *, preroll: bool, audio_mode: bool = False) -> None:
        self.state = State.OPEN
        self._frames = list(self._ring) if preroll else []
        self._ring.clear()
        self._opened_by = st
        self._units, self._changes = [], []
        self._continued = False
        self._audio_mode = audio_mode
        if st is not None:
            self._track(st)

    def _append(self, frame: AudioFrame) -> None:
        self._frames.append(frame)

    def _clip_start(self) -> int:
        return self._frames[0].sample if self._frames else self.now

    def _track(self, st: ScannerStatus) -> None:
        if st.unit_id and st.unit_id not in self._units:
            self._units.append(st.unit_id)
        last = self._changes[-1]["fields"] if self._changes else {}
        fields = {k: getattr(st, k) for k in _TRACKED}
        if fields != last:
            self._changes.append({"sample": self.now, "wall": st.wall.isoformat(),
                                  "fields": fields})

    def _split_at_handoff(self, st: ScannerStatus) -> Clip | None:
        """New unit while unmuted: cut at the start of the last silence (≥ unit_gap) and
        start a new clip for `st`. Returns the finished first part, or None if the first
        part is too short to be its own transmission (then `st` just joins this clip)."""
        pcm = np.concatenate([f.pcm for f in self._frames]) if self._frames else None
        if pcm is None:
            return None
        base, wall0 = self._frames[0].sample, self._frames[0].wall
        cut = len(pcm)
        resume = len(pcm)
        zero = pcm == 0
        # scan backwards for the last run of zeros at least unit_gap long
        i = len(pcm)
        while i > 0:
            if not zero[i - 1]:
                i -= 1
                continue
            j = i
            while j > 0 and zero[j - 1]:
                j -= 1
            if i - j >= self._unit_gap:
                cut, resume = j, i
                break
            i = j
        nz = np.flatnonzero(pcm[:cut])
        if nz.size == 0 or (nz[-1] + 1 - nz[0]) < self._min:
            return None
        # keep `pad` of the silence on each side, as for any other clip edge
        head_end = min(cut + self._pad, resume)
        tail_start = max(resume - self._pad, head_end)
        head = [AudioFrame(base, pcm[:head_end], wall0)]
        tail = AudioFrame(base + tail_start, pcm[tail_start:],
                          wall0 + timedelta(seconds=tail_start / SCANNER_RATE))
        self._frames = head
        clip = self._emit()
        self._ring.clear()
        self._open(st, preroll=False)
        if len(tail.pcm):
            self._frames.append(tail)
        return clip

    def _split_continued(self) -> list[Clip]:
        opened_by = self._opened_by
        audio_mode = self._audio_mode
        clip = self._emit()
        self._open(opened_by if self.status is None else self.status, preroll=False,
                   audio_mode=audio_mode)
        self._continued = True
        return [clip] if clip else []

    def _emit_list(self) -> list[Clip]:
        clip = self._emit()
        return [clip] if clip else []

    def _emit(self) -> Clip | None:
        frames, opened_by = self._frames, self._opened_by
        units, changes = self._units, self._changes
        continued, audio_mode = self._continued, self._audio_mode
        self.state = State.IDLE
        self._frames, self._opened_by = [], None
        self._units, self._changes = [], []
        if frames:  # the tail becomes pre-roll for whatever comes next
            self._ring.extend(frames[-max(1, self._preroll // max(len(frames[-1].pcm), 1)):])
        if not frames:
            return None
        pcm = np.concatenate([f.pcm for f in frames])
        nz = np.flatnonzero(pcm)
        if nz.size == 0 or (nz[-1] + 1 - nz[0]) < self._min:
            return None
        lo = max(0, int(nz[0]) - self._pad)
        hi = min(len(pcm), int(nz[-1]) + 1 + self._pad)
        start = frames[0].sample + lo
        return Clip(
            start_sample=start,
            end_sample=frames[0].sample + hi,
            pcm=pcm[lo:hi].copy(),
            started_at=frames[0].wall + timedelta(seconds=lo / SCANNER_RATE),
            status=opened_by,
            unit_ids=units,
            status_changes=changes,
            continued=continued,
            status_missing=audio_mode,
        )
