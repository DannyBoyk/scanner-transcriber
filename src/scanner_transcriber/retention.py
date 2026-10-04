"""Retention: keeps the disk bounded. Runs inside `run` every hour (and via the `retention`
CLI, with --dry-run to preview).

Everything is selected by each row's `started_at` (UTC), never by clip folder names (folders
use local dates). Work is done in small batches so a large first run never holds the database
for long. Deletes nothing that isn't covered by a setting; 0 means keep forever."""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import auth, statestore
from .config import Config
from .db import utcnow

log = logging.getLogger(__name__)
BATCH = 500
IN_FLIGHT = ("queued", "processing")


@dataclass
class Report:
    dry_run: bool
    audio_clips: int = 0
    audio_bytes: int = 0
    raw_trimmed: int = 0
    rows_deleted: int = 0
    events_deleted: int = 0
    commands_deleted: int = 0
    prompt_tests_deleted: int = 0
    missing_files: int = 0
    vacuumed: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def anything(self) -> bool:
        return any((self.audio_clips, self.raw_trimmed, self.rows_deleted, self.events_deleted,
                    self.commands_deleted, self.prompt_tests_deleted, self.vacuumed))

    def summary(self) -> str:
        parts = []
        if self.audio_clips:
            parts.append(f"audio of {self.audio_clips} clips ({self.audio_bytes / 1e6:.0f} MB)")
        if self.rows_deleted:
            parts.append(f"{self.rows_deleted} transmissions")
        if self.raw_trimmed:
            parts.append(f"raw scanner data on {self.raw_trimmed} rows")
        if self.events_deleted:
            parts.append(f"{self.events_deleted} events")
        if self.commands_deleted:
            parts.append(f"{self.commands_deleted} commands")
        if self.prompt_tests_deleted:
            parts.append(f"{self.prompt_tests_deleted} prompt-test results")
        text = ("would remove " if self.dry_run else "removed ") + ", ".join(parts) if parts \
            else "nothing to remove"
        if self.vacuumed:
            text += "; database compacted"
        return text


def _cutoff(days: int, now: datetime) -> str | None:
    return None if days <= 0 else (now - timedelta(days=days)).isoformat(timespec="milliseconds")


def _delete_file(data_dir: Path, rel: str, rep: Report) -> None:
    path = (data_dir / rel).resolve()
    if data_dir.resolve() not in path.parents:
        rep.notes.append(f"refused to delete outside data dir: {rel}")
        return
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        rep.missing_files += 1
        return
    if not rep.dry_run:
        path.unlink(missing_ok=True)
    rep.audio_bytes += size


def _prune_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p != root and not filenames and not any((p / d).exists() for d in dirnames):
            try:
                p.rmdir()
            except OSError:
                pass


def run(conn: sqlite3.Connection, cfg: Config, *, dry_run: bool = False,
        now: datetime | None = None, vacuum: bool = False) -> Report:
    r = cfg.retention
    now = now or datetime.now(UTC)
    data_dir = cfg.storage.data_dir
    rep = Report(dry_run=dry_run)

    def batches(select_sql: str, args: tuple):
        """Real run: repeatedly take the first BATCH matches (processed rows stop matching).
        Dry run: walk every match once (nothing changes, so no batching needed)."""
        if dry_run:
            rows = conn.execute(select_sql, args).fetchall()
            for i in range(0, len(rows), BATCH):
                yield rows[i:i + BATCH]
            return
        while True:
            rows = conn.execute(select_sql + " LIMIT ?", (*args, BATCH)).fetchall()
            if not rows:
                return
            yield rows
            if len(rows) < BATCH:
                return

    # 1. whole rows older than transcript_days (their audio file goes too)
    row_cut = cut = _cutoff(r.transcript_days, now)
    # later steps skip rows step 1 removes (matters for dry runs, where nothing is deleted)
    younger = " AND started_at >= ?" if row_cut else ""
    younger_args: tuple = (row_cut,) if row_cut else ()
    if cut:
        sql = (f"SELECT id, audio_path FROM transmissions WHERE started_at < ? "
               f"AND status NOT IN {IN_FLIGHT} ORDER BY started_at")
        for rows in batches(sql, (cut,)):
            for _, rel in rows:
                if rel:
                    _delete_file(data_dir, rel, rep)
            rep.rows_deleted += len(rows)
            if not dry_run:
                ids = [x[0] for x in rows]
                conn.execute(f"DELETE FROM transmissions WHERE id IN ({','.join('?' * len(ids))})", ids)
                conn.commit()

    # 2. audio older than audio_days (row and transcript kept)
    cut = _cutoff(r.audio_days, now)
    if cut:
        sql = (f"SELECT id, audio_path FROM transmissions WHERE audio_path IS NOT NULL "
               f"AND started_at < ?{younger} AND status NOT IN {IN_FLIGHT} ORDER BY started_at")
        for rows in batches(sql, (cut, *younger_args)):
            for _, rel in rows:
                _delete_file(data_dir, rel, rep)
            rep.audio_clips += len(rows)
            if not dry_run:
                ids = [x[0] for x in rows]
                conn.execute(f"UPDATE transmissions SET audio_path = NULL WHERE id IN "
                             f"({','.join('?' * len(ids))})", ids)
                conn.commit()
        if not dry_run:
            _prune_empty_dirs(data_dir / "clips")

    # 3. raw scanner data older than raw_days
    cut = _cutoff(r.raw_days, now)
    if cut:
        where = (f"started_at < ?{younger} AND status NOT IN {IN_FLIGHT} "
                 "AND (raw_status IS NOT NULL OR status_changes IS NOT NULL)")
        if dry_run:
            rep.raw_trimmed = conn.execute(f"SELECT COUNT(*) FROM transmissions WHERE {where}",
                                           (cut, *younger_args)).fetchone()[0]
        else:
            while True:
                cur = conn.execute(f"UPDATE transmissions SET raw_status = NULL, status_changes = NULL "
                                   f"WHERE id IN (SELECT id FROM transmissions WHERE {where} LIMIT ?)",
                                   (cut, *younger_args, BATCH))
                conn.commit()
                rep.raw_trimmed += cur.rowcount
                if cur.rowcount < BATCH:
                    break

    # 4. small tables
    def prune(sql_count: str, sql_delete: str, args: tuple) -> int:
        if dry_run:
            return conn.execute(sql_count, args).fetchone()[0]
        n = conn.execute(sql_delete, args).rowcount
        conn.commit()
        return n

    cut = _cutoff(r.event_days, now)
    if cut:
        rep.events_deleted = prune("SELECT COUNT(*) FROM system_events WHERE at < ?",
                                   "DELETE FROM system_events WHERE at < ?", (cut,))
    cut = _cutoff(r.command_days, now)
    if cut:
        rep.commands_deleted = prune(
            "SELECT COUNT(*) FROM commands WHERE done_at IS NOT NULL AND created_at < ?",
            "DELETE FROM commands WHERE done_at IS NOT NULL AND created_at < ?", (cut,))
    if r.prompt_tests_keep > 0:
        keep = ("id NOT IN (SELECT id FROM prompt_evals ORDER BY id DESC LIMIT ?) "
                "AND status NOT IN ('queued', 'running')")
        rep.prompt_tests_deleted = prune(f"SELECT COUNT(*) FROM prompt_evals WHERE {keep}",
                                         f"DELETE FROM prompt_evals WHERE {keep}",
                                         (r.prompt_tests_keep,))

    if not dry_run:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _daily_optimize(conn, now)
        if vacuum:
            rep.vacuumed = _vacuum(conn, cfg, rep)
        summary = {"at": utcnow(), "summary": rep.summary(), **asdict(rep)}
        auth.set_setting(conn, "retention_last_run", json.dumps(summary))
        if rep.anything:
            statestore.event(conn, "host", "info", "retention", f"Retention: {rep.summary()}")
        conn.commit()
        if rep.anything:
            log.info("retention: %s", rep.summary())
    return rep


def _daily_optimize(conn: sqlite3.Connection, now: datetime) -> None:
    last = auth.get_setting(conn, "last_optimize")
    if last and now - datetime.fromisoformat(last) < timedelta(hours=23):
        return
    conn.execute("PRAGMA optimize")
    auth.set_setting(conn, "last_optimize", now.isoformat())


def _vacuum(conn: sqlite3.Connection, cfg: Config, rep: Report) -> bool:
    """Compact the file (only on request: `retention --vacuum`). Not automatic: VACUUM holds
    the write lock for seconds on a year of data, and once retention reaches steady state
    SQLite reuses the freed pages, so the file stops growing anyway."""
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    size = pages * conn.execute("PRAGMA page_size").fetchone()[0]
    if shutil.disk_usage(cfg.storage.data_dir).free < size * 1.5:
        rep.notes.append("compaction skipped: not enough free disk for the temporary copy")
        return False
    conn.commit()
    conn.execute("VACUUM")
    return True


def last_run(conn: sqlite3.Connection) -> dict | None:
    v = auth.get_setting(conn, "retention_last_run")
    return json.loads(v) if v else None
