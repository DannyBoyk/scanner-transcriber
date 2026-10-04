"""SQLite connection, migrations and thin repository functions (no ORM)."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from .models import Clip, ScannerStatus


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds")


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)  # explicit transactions only
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL + NORMAL: no fsync per commit (batched at checkpoints). Can't corrupt the DB; a
    # sudden power cut may lose the last second or so of commits. Cuts SSD writes a lot.
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # capture and the worker wait up to 15 s for another writer before giving up (and capture
    # then retries a clip save a few more times, see app.save_all)
    conn.execute("PRAGMA busy_timeout = 15000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply numbered migrations/NNNN_*.sql not yet recorded in schema_version."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    files = sorted(
        (int(f.name.split("_", 1)[0]), f)
        for f in resources.files("scanner_transcriber.migrations").iterdir()
        if f.name.endswith(".sql")
    )
    for version, f in files:
        if version <= current:
            continue
        conn.execute("BEGIN")
        try:
            for stmt in _split_sql(f.read_text()):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        current = version
    return current


def _split_sql(script: str) -> list[str]:
    """Split on semicolons at statement ends, keeping CREATE TRIGGER ... END; whole."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        if line.lstrip().startswith("--") and not buf.strip():
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def channel_key(st: ScannerStatus | None) -> str | None:
    if st is None or not (st.system or st.tgid or st.channel):
        return None
    if st.tgid:
        return f"sys:{st.system}|tg:{st.tgid}"
    return f"sys:{st.system}|ch:{st.channel}|f:{st.frequency_hz}"


def upsert_channel(conn: sqlite3.Connection, st: ScannerStatus | None, heard_at: str) -> str | None:
    key = channel_key(st)
    if key is None:
        return None
    conn.execute(
        """INSERT INTO channels (key, system, department, channel, tgid, frequency_hz,
                                 first_heard_at, last_heard_at, color)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, (SELECT COUNT(*) FROM channels) % 10)
           ON CONFLICT(key) DO UPDATE SET
               last_heard_at = MAX(last_heard_at, excluded.last_heard_at),
               department = COALESCE(excluded.department, department),
               channel = COALESCE(excluded.channel, channel)""",
        (key, st.system, st.department, st.channel, st.tgid,
         None if st.tgid else st.frequency_hz, heard_at, heard_at),
    )
    return key


def insert_transmission(conn: sqlite3.Connection, clip: Clip, *, scanner_id: str,
                        audio_format: str = "wav") -> int:
    """Insert a clip row: 'queued' for transcription, or 'skipped' on an ignored channel."""
    st = clip.status
    started = iso(clip.started_at)
    key = upsert_channel(conn, st, started)
    priority, status, error = 0, "queued", None
    if key:
        row = conn.execute("SELECT priority, ignored FROM channels WHERE key = ?",
                           (key,)).fetchone()
        if row:
            priority = row["priority"]
            if row["ignored"]:
                status, error = "skipped", "ignored channel"
    now = utcnow()
    label = st.label() if st else {}
    cur = conn.execute(
        """INSERT INTO transmissions (
               scanner_id, channel_key, started_at, ended_at, duration_ms,
               system, department, channel, tgid, unit_id, unit_ids, site, frequency_hz,
               modulation, rssi, continued, status_missing, status_changes, raw_status,
               audio_format, status, error, priority, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (scanner_id, key, started, iso(clip.ended_at), clip.duration_ms,
         label.get("system"), label.get("department"), label.get("channel"), label.get("tgid"),
         clip.unit_ids[0] if clip.unit_ids else None, json.dumps(clip.unit_ids),
         label.get("site"), label.get("frequency_hz"), label.get("modulation"),
         label.get("rssi"), int(clip.continued), int(clip.status_missing),
         json.dumps(clip.status_changes), st.raw_xml if st else None,
         audio_format, status, error, priority, now, now),
    )
    return cur.lastrowid


def set_audio_path(conn: sqlite3.Connection, tx_id: int, rel_path: str) -> None:
    conn.execute("UPDATE transmissions SET audio_path = ?, updated_at = ? WHERE id = ?",
                 (rel_path, utcnow(), tx_id))


def recent(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM transmissions ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()
