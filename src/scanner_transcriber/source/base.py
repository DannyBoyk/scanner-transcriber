from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from ..models import AudioFrame, ScannerStatus

SourceEvent = AudioFrame | ScannerStatus


class ScannerSource(Protocol):
    """Where audio and status come from (live scanner or a recorded session).

    Differs from design §5.1: one merged `events()` stream instead of separate audio and
    status iterators, so ordering between the two is decided in one place. Statuses carry
    their position on the audio timeline (`sample`).
    """

    def events(self) -> AsyncIterator[SourceEvent]: ...

    async def close(self) -> None: ...  # live sources MUST send RTSP TEARDOWN
