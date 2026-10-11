from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

from .audio import SCANNER_RATE


@dataclass(frozen=True)
class ScannerStatus:
    """One normalized PSI/GSI status update (fields verified on SDS200 fw 1.21.00)."""

    mono: float                 # monotonic receive time
    wall: datetime              # UTC receive time
    sample: float | None        # position on the audio timeline; None if not yet mappable
    mode: str | None = None
    system: str | None = None
    system_type: str | None = None
    department: str | None = None
    channel: str | None = None  # talkgroup alpha tag
    tgid: str | None = None
    unit_id: str | None = None
    unit_name: str | None = None
    site: str | None = None
    frequency_hz: int | None = None
    modulation: str | None = None
    mute: str | None = None     # "Mute" / "Unmute"
    p25_status: str | None = None
    sig: int | None = None
    rssi: int | None = None
    raw_xml: str = ""

    @property
    def unmuted(self) -> bool:
        return self.mute == "Unmute"

    @classmethod
    def from_summary(cls, summary: dict, *, mono: float, wall: datetime,
                     sample: float | None, raw_xml: str = "") -> ScannerStatus:
        names = cls.__dataclass_fields__.keys() - {"mono", "wall", "sample", "raw_xml"}
        return cls(mono=mono, wall=wall, sample=sample, raw_xml=raw_xml,
                   **{k: summary.get(k) for k in names})

    def label(self) -> dict:
        """Fields that identify what was being heard (for DB rows / change tracking)."""
        return {k: getattr(self, k) for k in (
            "system", "department", "channel", "tgid", "unit_id", "site", "frequency_hz",
            "modulation", "rssi")}


@dataclass(frozen=True)
class AudioFrame:
    sample: int                 # index of the first sample on the audio timeline
    pcm: np.ndarray             # int16 mono, 8 kHz
    wall: datetime              # UTC time of the first sample

    @property
    def end(self) -> int:
        return self.sample + len(self.pcm)


@dataclass
class Clip:
    start_sample: int
    end_sample: int
    pcm: np.ndarray
    started_at: datetime        # UTC wall time of the first sample of `pcm`
    status: ScannerStatus | None  # status that opened the clip
    unit_ids: list[str] = field(default_factory=list)
    status_changes: list[dict] = field(default_factory=list)
    continued: bool = False     # continuation of a transmission split at max_clip_s
    status_missing: bool = False  # cut from audio alone while status was stale

    @property
    def duration_ms(self) -> int:
        return len(self.pcm) * 1000 // SCANNER_RATE

    @property
    def ended_at(self) -> datetime:
        return self.started_at + timedelta(seconds=len(self.pcm) / SCANNER_RATE)
