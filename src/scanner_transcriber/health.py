"""One health evaluator for everything that reports status: the web header dot, /status,
/api/status, /healthz and the CLI `status` command all call `evaluate()`.

Pure where it matters: `assess()` takes plain data and a clock, so every threshold is
table-testable; `evaluate()` just gathers that data from the DB and the host."""
from __future__ import annotations

import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import statestore
from .config import Config, StatusConfig

LEVELS = ("ok", "warn", "error")


@dataclass
class Condition:
    component: str        # capture | worker | queue | host
    level: str            # warn | error
    kind: str             # stable id, e.g. "reconnecting", "degraded", "backlog", "disk_low"
    message: str          # shown in the dot's tooltip and on /status


@dataclass
class Health:
    level: str
    conditions: list[Condition]
    components: dict
    queue: dict
    host: dict
    checked_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def as_dict(self) -> dict:
        return asdict(self)


def _age(ts: str | None, now: datetime) -> float | None:
    return None if not ts else (now - datetime.fromisoformat(ts)).total_seconds()


def _dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def assess(components: dict, queue: dict, host: dict, cfg: StatusConfig,
           now: datetime) -> list[Condition]:
    out: list[Condition] = []

    # --- capture / scanner -------------------------------------------------------------
    cap = components.get("capture")
    if cap is None:
        out.append(Condition("capture", "error", "capture_never_ran",
                             "Capture has never run: the scanner isn't being recorded"))
    elif cap["state"] == "stopped":
        out.append(Condition("capture", "error", "capture_stopped",
                             "Capture is stopped: the scanner isn't being recorded"))
    elif (_age(cap["updated_at"], now) or 0) > cfg.capture_stale_s:
        out.append(Condition("capture", "error", "capture_down",
                             "Capture process isn't running (no heartbeat for "
                             f"{_dur(_age(cap['updated_at'], now))})"))
    else:
        d = cap["detail"]
        if cap["state"] == "reconnecting" or (cap["state"] == "connecting"
                                              and d.get("disconnected_at")):
            since = _age(d.get("disconnected_at"), now) or 0
            retry = _age(d.get("next_retry_at"), now)
            msg = (f"Scanner unreachable for {_dur(since)} — reconnecting"
                   + (f", next try in {_dur(-retry)}" if retry is not None and retry < 0 else ""))
            if d.get("last_error"):
                msg += f" ({d['last_error']})"
            level = "error" if since >= cfg.reconnect_error_s else "warn"
            out.append(Condition("capture", level, "reconnecting", msg))
        elif cap["state"] == "rebooting":
            since = _age(d.get("disconnected_at"), now) or 0
            out.append(Condition("capture", "warn", "rebooting",
                                 f"Rebooting the scanner to recover its audio (no audio for "
                                 f"{_dur(since)})"))
        elif cap["state"] == "needs_power_cycle":
            out.append(Condition("capture", "error", "needs_power_cycle",
                                 "Scanner audio is stuck and automatic reboots didn't help: "
                                 "power-cycle the scanner"
                                 + (f" ({d['last_error']})" if d.get("last_error") else "")))
        elif cap["state"] == "session_busy":
            out.append(Condition("capture", "error", "session_busy",
                                 "Scanner audio is in use by another program "
                                 f"({d.get('last_error', 'session busy')}); retrying"))

    # --- worker ------------------------------------------------------------------------
    w = components.get("worker")
    if w is None or w["state"] == "stopped":
        out.append(Condition("worker", "error", "worker_down",
                             "Transcription worker isn't running"))
    elif (_age(w["updated_at"], now) or 0) > cfg.worker_stale_s:
        out.append(Condition("worker", "error", "worker_down",
                             "Transcription worker isn't responding (no heartbeat for "
                             f"{_dur(_age(w['updated_at'], now))})"))
    elif w["state"] == "degraded":
        out.append(Condition("worker", "warn", "degraded",
                             "Transcribing in degraded mode (CPU fallback model, lower accuracy): "
                             + str(w["detail"].get("degraded_reason") or "GPU unavailable")))
    if queue.get("failed_recent"):
        out.append(Condition("worker", "warn", "failed_clips",
                             f"{queue['failed_recent']} clip(s) failed to transcribe in the last "
                             f"{cfg.failed_window_h:g}h"))

    # --- falling behind ----------------------------------------------------------------
    wait = queue.get("oldest_queued_age_s")
    if wait is not None and wait > cfg.backlog_warn_s:
        level = "error" if wait > cfg.backlog_error_s else "warn"
        out.append(Condition("queue", level, "backlog",
                             f"Transcription is behind: {queue.get('queued', '?')} clip(s) waiting, "
                             f"oldest {_dur(wait)}"))

    # --- host: disk ----------------------------------------------------------------------
    free_gb, pct = host.get("disk_free_gb"), host.get("disk_free_pct")
    if free_gb is not None:
        if free_gb < cfg.disk_error_gb:
            out.append(Condition("host", "error", "disk_low",
                                 f"Disk almost full: {free_gb:.1f} GB free — recording may fail"))
        elif free_gb < cfg.disk_warn_gb or pct < cfg.disk_warn_pct:
            out.append(Condition("host", "warn", "disk_low",
                                 f"Disk getting full: {free_gb:.1f} GB ({pct:.0f}%) free"))
    return out


def overall(conditions: list[Condition]) -> str:
    return max((c.level for c in conditions), key=LEVELS.index, default="ok")


def host_info(data_dir: Path) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    du = shutil.disk_usage(data_dir)
    info = {"disk_free_gb": round(du.free / 1e9, 2), "disk_free_pct": round(100 * du.free / du.total, 1),
            "disk_total_gb": round(du.total / 1e9, 1)}
    temps = []
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            temps.append(int((zone / "temp").read_text()) / 1000)
        except (OSError, ValueError):
            pass
    info["cpu_temp_c"] = max(temps) if temps else None
    try:
        mhz = [float(line.split(":")[1]) for line in Path("/proc/cpuinfo").read_text().splitlines()
               if line.startswith("cpu MHz")]
        info["cpu_mhz"] = round(sum(mhz) / len(mhz)) if mhz else None
    except OSError:
        info["cpu_mhz"] = None
    try:
        info["uptime_s"] = int(float(Path("/proc/uptime").read_text().split()[0]))
    except OSError:
        info["uptime_s"] = None
    return info


def queue_info(conn: sqlite3.Connection, cfg: StatusConfig, now: datetime) -> dict:
    by_status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM transmissions GROUP BY status").fetchall())
    oldest = conn.execute(
        "SELECT MIN(ended_at) FROM transmissions WHERE status = 'queued'").fetchone()[0]
    since = (now - timedelta(hours=cfg.failed_window_h)).isoformat()
    failed = conn.execute(
        "SELECT COUNT(*) FROM transmissions WHERE status = 'failed' AND updated_at >= ?",
        (since,)).fetchone()[0]
    return {"by_status": by_status, "queued": by_status.get("queued", 0),
            "oldest_queued_age_s": _age(oldest, now), "failed_recent": failed}


def evaluate(conn: sqlite3.Connection, cfg: Config, now: datetime | None = None) -> Health:
    now = now or datetime.now(UTC)
    components = statestore.states(conn)
    queue = queue_info(conn, cfg.status, now)
    host = host_info(cfg.storage.data_dir)
    conds = assess(components, queue, host, cfg.status, now)
    return Health(level=overall(conds), conditions=conds, components=components,
                  queue=queue, host=host, checked_at=now.isoformat())
