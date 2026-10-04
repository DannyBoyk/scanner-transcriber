"""Status publishing for the `run` process: the capture heartbeat + connection events, and a
monitor that logs transitions of derived conditions (backlog, disk, failed clips)."""
from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from . import health, statestore
from .config import Config
from .models import AudioFrame, ScannerStatus

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _dur(seconds: float) -> str:
    s = int(seconds)
    return f"{s}s" if s < 60 else f"{s // 60}m {s % 60:02d}s" if s < 3600 else \
        f"{s // 3600}h {s % 3600 // 60:02d}m"


class CaptureReporter:
    """Tracks the capture side's state and writes it to `component_state` / `system_events`.

    Sources call `on_source_state()`; the pipeline calls `saw_audio()` / `saw_status()`;
    `publish()` is called every 5 s by a timer (and on every connection change), even while the source is stuck
    reconnecting, so a stale heartbeat really means the process is gone."""

    def __init__(self, conn: sqlite3.Connection, *, source_kind: str = "live",
                 source_stats=None) -> None:
        self.conn = conn
        self.source_kind = source_kind
        self.source_stats = source_stats  # object with .reconnects / .packets_lost (live source)
        self.state = "connecting" if source_kind == "live" else "replaying"
        self.detail: dict = {"source": source_kind, "started_at": _now().isoformat()}
        self._ever_connected = False
        self._disconnected_at: datetime | None = None

    # -- inputs ----------------------------------------------------------------------------
    def on_source_state(self, state: str, **info) -> None:
        """An outage starts at the first failure (reconnecting / session_busy) and ends at the
        next `streaming`. One event when it starts, one when it ends, plus one if it turns
        into session_busy part-way through."""
        now = _now()
        d = self.detail
        if "error" in info:
            d["last_error"] = info["error"]
        if "next_retry_at" in info:
            d["next_retry_at"] = info["next_retry_at"].isoformat()

        if state == "streaming":
            if self._disconnected_at is not None:
                outage = (now - self._disconnected_at).total_seconds()
                statestore.event(self.conn, "capture", "info", "reconnected",
                                 f"Scanner reconnected after {_dur(outage)}",
                                 {"outage_s": round(outage, 1)})
            elif not self._ever_connected:
                statestore.event(self.conn, "capture", "info", "connected", "Scanner connected")
            self._ever_connected = True
            self._disconnected_at = None
            for k in ("disconnected_at", "next_retry_at", "last_error"):
                d.pop(k, None)
            d["connected_since"] = now.isoformat()

        elif state == "rebooting":
            # part of an outage (it started when audio was lost), or a fresh one (Admin)
            if self._disconnected_at is None:
                self._disconnected_at = now
                d["disconnected_at"] = now.isoformat()
                d.pop("connected_since", None)
            d["reboots"] = d.get("reboots", 0) + 1
            d["last_reboot_at"] = now.isoformat()
            ack = "" if info.get("acknowledged", True) else " (no acknowledgement from the scanner)"
            if info.get("auto"):
                statestore.event(self.conn, "capture", "warn", "scanner_reboot",
                                 f"Scanner audio stuck: rebooting the scanner "
                                 f"({info.get('error')}){ack}")
            else:
                statestore.event(self.conn, "capture", "info", "scanner_reboot",
                                 f"Rebooting the scanner ({info.get('error')}){ack}")

        elif state == "needs_power_cycle":
            statestore.event(self.conn, "capture", "error", "needs_power_cycle",
                             f"Scanner audio is stuck and automatic reboots didn't help "
                             f"({info.get('error')}): power-cycle the scanner")

        elif state in ("reconnecting", "session_busy"):
            if self._disconnected_at is None:
                self._disconnected_at = now
                d["disconnected_at"] = now.isoformat()
                d.pop("connected_since", None)
                if state == "session_busy":
                    statestore.event(self.conn, "capture", "error", "session_busy",
                                     f"Scanner audio unavailable: {info.get('error')}")
                elif self._ever_connected:
                    statestore.event(self.conn, "capture", "warn", "disconnected",
                                     f"Scanner connection lost: {info.get('error')}")
                else:
                    statestore.event(self.conn, "capture", "warn", "disconnected",
                                     f"Can't reach the scanner: {info.get('error')}")
            elif state == "session_busy" and self.state != "session_busy":
                statestore.event(self.conn, "capture", "error", "session_busy",
                                 f"Scanner audio unavailable: {info.get('error')}")

        self.state = state
        self.publish()

    def saw_audio(self, frame: AudioFrame) -> None:
        self.detail["last_audio_at"] = frame.wall.isoformat()

    def saw_status(self, st: ScannerStatus) -> None:
        self.detail["last_status_at"] = st.wall.isoformat()
        if st.unmuted and st.tgid:
            self.detail["current"] = {"tgid": st.tgid, "channel": st.channel,
                                      "department": st.department, "unit_id": st.unit_id}
        elif not st.unmuted:
            self.detail["current"] = None

    # -- outputs ---------------------------------------------------------------------------
    def publish(self) -> None:
        if self.source_stats is not None:
            self.detail["reconnects"] = getattr(self.source_stats, "reconnects", 0)
            lost = getattr(self.source_stats, "packets_lost", 0)
            session = getattr(self.source_stats, "session", None)
            audio = getattr(session, "audio", None)
            if audio is not None:
                lost += audio.statistics.packets_lost
            self.detail["rtp_packets_lost"] = lost
        statestore.publish(self.conn, "capture", self.state, self.detail)

    def started(self) -> None:
        statestore.event(self.conn, "capture", "info", "started",
                         f"Capture started ({self.source_kind})")
        self.publish()

    def stopped(self, reason: str = "clean shutdown") -> None:
        self.state = "stopped"
        self.detail["stopped_at"] = _now().isoformat()
        statestore.publish(self.conn, "capture", "stopped", self.detail)
        statestore.event(self.conn, "capture", "info", "stopped", f"Capture stopped ({reason})")


class HealthMonitor:
    """Logs transitions of conditions that no single component owns: backlog, disk space,
    failed clips. (Capture and worker log their own transitions.)"""

    DERIVED = {"backlog", "disk_low", "failed_clips"}

    def __init__(self, conn: sqlite3.Connection, cfg: Config) -> None:
        self.conn = conn
        self.cfg = cfg
        self.active: dict[str, tuple[str, str]] = {}  # kind -> (level, component)

    def check(self, now: datetime | None = None) -> None:
        h = health.evaluate(self.conn, self.cfg, now)
        current = {c.kind: c for c in h.conditions if c.kind in self.DERIVED}
        for kind, c in current.items():
            prev = self.active.get(kind)
            if prev is None or prev[0] != c.level:
                statestore.event(self.conn, c.component, c.level, kind, c.message)
                self.active[kind] = (c.level, c.component)
        for kind in list(self.active):
            if kind not in current:
                _, component = self.active.pop(kind)
                statestore.event(self.conn, component, "info", f"{kind}_cleared",
                                 {"backlog": "Transcription caught up",
                                  "disk_low": "Disk space OK again",
                                  "failed_clips": "No recent transcription failures"}[kind])
