"""Listen live (M6, docs/m6-listen-live-plan.md): one continuous MP3 stream of everything the
scanner hears, which phones keep playing with the screen locked.

One shared Station (an asyncio task in the web process) turns new clips into MP3 and fans the
chunks out to every listener. Nothing runs while nobody listens:

    capture saves clip WAV + row ──▶ Station: poll new rows → level → 16 kHz → MP3 ─┬─▶ listener
                                     (silence when idle, paced to real time)        ├─▶ listener
                                                                                    └─▶ …

Each listener is a bounded queue behind an endless HTTP response (GET /stream.mp3)."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path

import lameenc
import numpy as np

from ..audio import SCANNER_RATE, level, read_wav, resample

log = logging.getLogger(__name__)

STREAM_RATE = 16000      # MPEG-2 Layer III: Safari doesn't reliably play 8 kHz MP3 (MPEG-2.5)
STREAM_KBPS = 32

# Live-stream response headers: no length, no ranges. Safari probes media with
# "Range: bytes=0-1"; answering that with 2 bytes makes it give up, so (like Icecast) the probe
# gets 200 and the endless stream.
HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Content-Type": "audio/mpeg"}


class Mp3Encoder:
    """Mono 16-bit PCM at STREAM_RATE → MP3 frames (LAME, constant bit rate)."""

    def __init__(self, rate: int = STREAM_RATE, kbps: int = STREAM_KBPS) -> None:
        self._enc = lameenc.Encoder()
        self._enc.set_bit_rate(kbps)
        self._enc.set_in_sample_rate(rate)
        self._enc.set_channels(1)
        self._enc.set_quality(5)        # 2 = best … 7 = fastest; speech at 32 kbit/s needs little

    def encode(self, pcm16: np.ndarray) -> bytes:
        return bytes(self._enc.encode(pcm16.astype("<i2").tobytes()))


class Listener:
    def __init__(self, maxsize: int, key: str | None = None) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=maxsize)
        self.dropped = False
        self.key = key          # the page's per-connection token (?t=…); None for VLC etc.


class Station:
    """The shared live stream. `listen()` is the body of one HTTP response."""

    def __init__(self, db_path: Path, data_dir: Path, *, chunk_s: float = 0.25,
                 ahead_s: float = 2.0, poll_s: float = 0.5, pause_s: float = 0.4,
                 idle_stop_s: float = 30.0, backlog_s: float = 20.0, intro_s: float = 2.0,
                 max_pending_s: float = 600.0) -> None:
        self.db_path, self.data_dir = Path(db_path), Path(data_dir)
        self.chunk_s, self.ahead_s, self.poll_s, self.pause_s = chunk_s, ahead_s, poll_s, pause_s
        self.idle_stop_s = idle_stop_s
        self.max_pending = int(max_pending_s * STREAM_RATE)   # safety cap on queued audio
        chunks_per_s = 1 / chunk_s
        self.backlog_chunks = max(4, int(backlog_s * chunks_per_s))
        self.listeners: set[Listener] = set()
        self.ring: deque[bytes] = deque(maxlen=max(1, int(intro_s * chunks_per_s)))
        self.task: asyncio.Task | None = None
        self._idle_since: float | None = None
        self.started_at: float | None = None
        self.clips_played = 0
        self.last_id = 0

    # -- public ---------------------------------------------------------------------------------
    @property
    def state(self) -> str:
        return "playing" if self.task and not self.task.done() else "idle"

    @property
    def listener_count(self) -> int:
        """People listening, not connections: Safari opens the stream twice (a probe, then the
        real request) with the same page token, which must count once."""
        keyed = {lst.key for lst in self.listeners if lst.key}
        return len(keyed) + sum(1 for lst in self.listeners if not lst.key)

    def info(self) -> dict:
        return {"state": self.state, "listeners": self.listener_count,
                "clips_played": self.clips_played}

    async def listen(self, key: str | None = None) -> AsyncIterator[bytes]:
        """Join the stream: the last couple of seconds first (quick start), then live chunks.
        Ends when the client disconnects (the response is cancelled) or falls too far behind.
        `key` identifies one listener across its connections (see listener_count)."""
        me = Listener(self.backlog_chunks, key)
        joined = time.monotonic()
        log.info("listen live: listener joined (%s); %d connection(s) now", key or "no token",
                 len(self.listeners) + 1)
        for chunk in self.ring:
            me.queue.put_nowait(chunk)
        self.listeners.add(me)
        self._idle_since = None
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="listen-live-station")
        try:
            while True:
                chunk = await me.queue.get()
                if chunk is None:            # dropped: too far behind, or the station stopped
                    return
                yield chunk
        finally:
            self.listeners.discard(me)
            log.info("listen live: listener left after %.0f s (%s; %s)", time.monotonic() - joined,
                     key or "no token", "dropped: fell behind" if me.dropped else "connection closed")
            if not self.listeners:
                self._idle_since = time.monotonic()

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # -- the station loop -----------------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        c.execute("PRAGMA busy_timeout = 10000")
        return c

    def _newest_id(self) -> int:
        c = self._db()
        try:
            return c.execute("SELECT COALESCE(MAX(id), 0) FROM transmissions").fetchone()[0]
        finally:
            c.close()

    def _new_clips(self) -> list[tuple[int, str]]:
        """Rows saved since the last poll that have audio, oldest first. No filters: like the
        scanner's speaker, every talkgroup and every clip (also no-speech ones)."""
        c = self._db()
        try:
            return c.execute("SELECT id, audio_path FROM transmissions WHERE id > ? "
                             "AND audio_path IS NOT NULL ORDER BY id", (self.last_id,)).fetchall()
        finally:
            c.close()

    def _prepare(self, rel: str) -> np.ndarray | None:
        """Clip → levelled, 16 kHz PCM followed by the pause. None if the file is gone."""
        try:
            pcm, rate = read_wav(self.data_dir / rel)
        except (OSError, ValueError, EOFError):
            return None
        x = resample(level(pcm), rate or SCANNER_RATE, STREAM_RATE)     # float32 in [-1, 1]
        out = np.clip(np.round(x * 32767), -32768, 32767).astype(np.int16)
        return np.concatenate([out, np.zeros(int(self.pause_s * STREAM_RATE), np.int16)])

    def _broadcast(self, chunk: bytes) -> None:
        self.ring.append(chunk)
        for lst in list(self.listeners):
            if lst.dropped:
                continue
            try:
                lst.queue.put_nowait(chunk)
            except asyncio.QueueFull:        # a dead connection that hasn't closed: let it go
                lst.dropped = True
                self.listeners.discard(lst)
                while not lst.queue.empty():
                    lst.queue.get_nowait()
                lst.queue.put_nowait(None)
                log.info("listen live: dropped a listener that fell %.0f s behind",
                         self.backlog_chunks * self.chunk_s)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        enc = Mp3Encoder()
        self.started_at = time.monotonic()
        self.clips_played = 0
        self.ring.clear()
        try:
            self.last_id = await asyncio.to_thread(self._newest_id)   # "now": only new clips
        except sqlite3.Error:
            log.exception("listen live: database unavailable; starting from silence")
        log.info("listen live: station started (after transmission %d)", self.last_id)
        pending: deque[np.ndarray] = deque()          # prepared audio waiting to be played
        pending_n = 0
        step = int(self.chunk_s * STREAM_RATE)
        start = loop.time()
        produced = 0                                    # samples encoded so far
        next_poll = 0.0
        try:
            while True:
                now = loop.time()
                if self.listeners:
                    self._idle_since = None
                elif self._idle_since and time.monotonic() - self._idle_since >= self.idle_stop_s:
                    log.info("listen live: no listeners for %.0f s; station stopped", self.idle_stop_s)
                    return
                if now >= next_poll:                    # pick up newly saved clips
                    next_poll = now + self.poll_s
                    try:
                        for tx_id, rel in await asyncio.to_thread(self._new_clips):
                            self.last_id = max(self.last_id, tx_id)
                            audio = await asyncio.to_thread(self._prepare, rel)
                            if audio is None or pending_n + len(audio) > self.max_pending:
                                continue
                            pending.append(audio)
                            pending_n += len(audio)
                            self.clips_played += 1
                    except sqlite3.Error:
                        log.warning("listen live: couldn't read new clips; retrying", exc_info=True)
                ahead = produced / STREAM_RATE - (now - start)
                if ahead > self.ahead_s:
                    await asyncio.sleep(min(self.chunk_s, ahead - self.ahead_s, max(0.01, next_poll - now)))
                    continue
                # next chunk: queued clip audio, padded with silence when there isn't enough
                parts, need = [], step
                while need and pending:
                    head = pending[0]
                    take = head[:need]
                    parts.append(take)
                    need -= len(take)
                    if len(take) == len(head):
                        pending.popleft()
                    else:
                        pending[0] = head[len(take):]
                    pending_n -= len(take)
                if need:
                    parts.append(np.zeros(need, np.int16))
                mp3 = await asyncio.to_thread(enc.encode, np.concatenate(parts))
                produced += step
                if mp3:                                 # LAME holds back the first frames
                    self._broadcast(mp3)
        finally:
            for lst in list(self.listeners):           # station stopping: end every response
                try:
                    lst.queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
