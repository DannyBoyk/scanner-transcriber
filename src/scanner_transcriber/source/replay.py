"""Replays a `record` session directory (audio.wav + anchors.jsonl + status.jsonl)."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

from ..audio import SCANNER_RATE, read_wav
from ..models import AudioFrame, ScannerStatus
from ..timeline import AnchorClock
from .base import SourceEvent

FRAME = 320  # 40 ms, matching the scanner's RTP packets


class ReplaySource:
    kind = "replay"

    def __init__(self, session_dir: Path, *, speed: str = "max") -> None:
        if speed not in ("max", "realtime"):
            raise ValueError("speed must be 'max' or 'realtime'")
        self.dir = Path(session_dir)
        self.speed = speed
        self._closed = False

    def _load(self):
        pcm, rate = read_wav(self.dir / "audio.wav")
        if rate != SCANNER_RATE:
            raise ValueError(f"{self.dir}: expected {SCANNER_RATE} Hz audio")
        anchors = [json.loads(line) for line in (self.dir / "anchors.jsonl").open()]
        clock = AnchorClock([(a["mono"], a["sample"]) for a in anchors])
        session = json.loads((self.dir / "session.json").read_text()) \
            if (self.dir / "session.json").exists() else {}
        statuses = []
        for line in (self.dir / "status.jsonl").open():
            r = json.loads(line)
            statuses.append(ScannerStatus.from_summary(
                r["status"], mono=r["mono"], wall=datetime.fromisoformat(r["wall"]),
                sample=clock.to_sample(r["mono"]), raw_xml=r.get("xml", "")))
        # wall clock of sample 0: from session start, or failing that the first status
        if "started_wall" in session and "started_mono" in session:
            wall0 = (datetime.fromisoformat(session["started_wall"])
                     + timedelta(seconds=clock.to_mono(0) - session["started_mono"]))
        elif statuses:
            s = statuses[0]
            wall0 = s.wall - timedelta(seconds=(s.sample or 0) / SCANNER_RATE)
        else:
            raise ValueError(f"{self.dir}: no session.json or statuses to date the audio")
        return pcm, statuses, wall0

    async def events(self) -> AsyncIterator[SourceEvent]:
        pcm, statuses, wall0 = self._load()
        si = 0
        t_start = time.monotonic()
        for i in range(0, len(pcm), FRAME):
            if self._closed:
                return
            end = i + FRAME
            while si < len(statuses) and (statuses[si].sample or 0) <= end:
                yield statuses[si]
                si += 1
            yield AudioFrame(i, pcm[i:end], wall0 + timedelta(seconds=i / SCANNER_RATE))
            if self.speed == "realtime":
                delay = t_start + end / SCANNER_RATE - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
            elif i % (FRAME * 250) == 0:
                await asyncio.sleep(0)  # stay cooperative every ~10 s of audio
        for st in statuses[si:]:
            yield st

    async def close(self) -> None:
        self._closed = True
