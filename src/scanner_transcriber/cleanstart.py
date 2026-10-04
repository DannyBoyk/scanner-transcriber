"""Clean start (Admin page / `clean-start` CLI): delete all transmissions and their audio, the
event log and logs, so the system starts over. Kept: talkgroup settings, prompt and
substitution versions, labelled clips (data/sessions) and prompt tests, the admin password and
other settings, soak data, model caches.

Safe while capture and the worker run: only rows that existed when it started are deleted
(with their files), plus audio files older than the start that no row points to, so a clip
saved meanwhile survives intact."""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import statestore
from .config import Config
from .logsetup import COMPONENTS, log_path

log = logging.getLogger(__name__)

BATCH = 1000
AUDIO_DIRS = ("clips", "spill")


@dataclass
class Summary:
    transmissions: int = 0
    audio_files: int = 0
    audio_bytes: int = 0
    events: int = 0
    batches: int = 0
    log_files: int = 0
    errors: list[str] = field(default_factory=list)

    def text(self, dry_run: bool = False) -> str:
        verb = "would remove" if dry_run else "removed"
        return (f"{verb} {self.transmissions} transmissions, {self.audio_files} audio files "
                f"({self.audio_bytes / 1e6:.0f} MB), {self.events} events, "
                f"{self.batches} re-transcribe batches, {self.log_files} log files")


def _audio_files(data_dir: Path):
    for d in AUDIO_DIRS:
        root = data_dir / d
        if root.exists():
            yield from (p for p in root.rglob("*") if p.is_file())


def preview(conn: sqlite3.Connection, cfg: Config) -> Summary:
    s = Summary()
    s.transmissions = conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0]
    for p in _audio_files(cfg.storage.data_dir):
        s.audio_files += 1
        s.audio_bytes += p.stat().st_size
    s.events = conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0]
    s.batches = conn.execute("SELECT COUNT(*) FROM retranscribe_batches").fetchone()[0]
    s.log_files = sum(1 for c in COMPONENTS for p in log_path(cfg.storage.data_dir, c).parent.glob(f"{c}.log*"))
    return s


def run(conn: sqlite3.Connection, cfg: Config) -> Summary:
    """`conn` must be a db.connect() connection (autocommit mode, explicit transactions)."""
    data_dir = cfg.storage.data_dir
    started = time.time()
    s = Summary()
    max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM transmissions").fetchone()[0]

    # 1. rows that existed at the start, with their audio, in batches (FTS triggers fire per row)
    while True:
        rows = conn.execute("SELECT id, audio_path FROM transmissions WHERE id <= ? LIMIT ?",
                            (max_id, BATCH)).fetchall()
        if not rows:
            break
        for _, rel in rows:
            if rel:
                p = data_dir / rel
                try:
                    s.audio_bytes += p.stat().st_size
                    p.unlink()
                    s.audio_files += 1
                except FileNotFoundError:
                    pass
                except OSError as e:
                    s.errors.append(f"{rel}: {e}")
        ids = [r[0] for r in rows]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(f"DELETE FROM transmissions WHERE id IN ({','.join('?' * len(ids))})", ids)
        conn.execute("COMMIT")
        s.transmissions += len(ids)

    # 2. leftover audio from before the start that no row points to (old orphans, spill files)
    for p in list(_audio_files(data_dir)):
        try:
            st = p.stat()
            if st.st_mtime >= started:
                continue                        # a clip saved since we began: keep
            rel = str(p.relative_to(data_dir))
            if conn.execute("SELECT 1 FROM transmissions WHERE audio_path = ?", (rel,)).fetchone():
                continue
            p.unlink()
            s.audio_files += 1
            s.audio_bytes += st.st_size
        except OSError as e:
            s.errors.append(f"{p}: {e}")
    for d in AUDIO_DIRS:                        # empty date folders
        root = data_dir / d
        if root.exists():
            for sub in sorted((x for x in root.rglob("*") if x.is_dir()), reverse=True):
                try:
                    sub.rmdir()
                except OSError:
                    pass

    # 3. small tables
    conn.execute("BEGIN IMMEDIATE")
    s.batches = conn.execute("DELETE FROM retranscribe_batches").rowcount
    conn.execute("UPDATE transmissions SET batch_id = NULL WHERE batch_id IS NOT NULL")
    s.events = conn.execute("DELETE FROM system_events").rowcount
    conn.execute("DELETE FROM commands WHERE done_at IS NOT NULL")
    conn.execute("COMMIT")

    # 4. log files: truncate the live ones (handlers append), delete rotated copies
    for c in COMPONENTS:
        live = log_path(data_dir, c)
        for p in live.parent.glob(f"{c}.log*"):
            try:
                if p == live:
                    with open(p, "r+") as f:
                        f.truncate(0)
                else:
                    p.unlink()
                s.log_files += 1
            except OSError as e:
                s.errors.append(f"{p}: {e}")

    # 5. give the space back (writers wait up to 15 s; this takes seconds at most here)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("VACUUM")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # in WAL mode the file only shrinks here
    statestore.event(conn, "host", "warn", "clean_start", f"Clean start: {s.text()}"
                     + (f"; {len(s.errors)} errors (see the capture log)" if s.errors else ""))
    for e in s.errors[:20]:
        log.warning("clean start: %s", e)
    log.warning("clean start: %s", s.text())
    return s

