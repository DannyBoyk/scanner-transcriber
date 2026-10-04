"""The transcription queue: the `transmissions` table itself (durable across restarts)."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta

from .db import utcnow

CLAIM_COLUMNS = "id, audio_path, duration_ms, started_at, ended_at, tgid, attempts"


def claim(conn: sqlite3.Connection, worker_id: str) -> sqlite3.Row | None:
    """Atomically take the next queued clip: highest priority, then oldest."""
    now = utcnow()
    return conn.execute(
        f"""UPDATE transmissions
            SET status = 'processing', claimed_at = ?, claimed_by = ?,
                attempts = attempts + 1, updated_at = ?
            WHERE id = (SELECT id FROM transmissions WHERE status = 'queued'
                        ORDER BY priority DESC, started_at ASC LIMIT 1)
            RETURNING {CLAIM_COLUMNS}""",
        (now, worker_id, now),
    ).fetchone()


def finish(conn: sqlite3.Connection, tx_id: int, *, transcript: str, raw_transcript: str,
           alt_transcript: str | None, flags: list[str], no_speech: bool,
           stt_backend: str, stt_model: str, stt_ms: int, prompt_version: int | None = None,
           subs_version: int | None = None) -> None:
    now = utcnow()
    conn.execute(
        """UPDATE transmissions
           SET status = 'done', transcript = ?, raw_transcript = ?, alt_transcript = ?,
               flags = ?, no_speech = ?, stt_backend = ?, stt_model = ?, stt_ms = ?,
               prompt_version = ?, subs_version = ?,
               error = NULL, transcribed_at = ?, updated_at = ?
           WHERE id = ?""",
        (transcript, raw_transcript, alt_transcript, json.dumps(flags), int(no_speech),
         stt_backend, stt_model, stt_ms, prompt_version, subs_version, now, now, tx_id),
    )


def fail(conn: sqlite3.Connection, tx_id: int, error: str, *, max_attempts: int) -> str:
    """Re-queue after an error, or mark failed once attempts are used up. Returns new status."""
    row = conn.execute("SELECT attempts FROM transmissions WHERE id = ?", (tx_id,)).fetchone()
    status = "failed" if row is None or row["attempts"] >= max_attempts else "queued"
    conn.execute(
        """UPDATE transmissions SET status = ?, error = ?, claimed_at = NULL, claimed_by = NULL,
               updated_at = ? WHERE id = ?""",
        (status, error[:2000], utcnow(), tx_id),
    )
    return status


def recover_stale(conn: sqlite3.Connection, timeout_s: float) -> int:
    """Re-queue clips a crashed or killed worker left in 'processing'."""
    cutoff = (datetime.now(UTC) - timedelta(seconds=timeout_s)).isoformat(timespec="milliseconds")
    cur = conn.execute(
        """UPDATE transmissions SET status = 'queued', claimed_at = NULL, claimed_by = NULL,
               error = 'recovered after stale claim', updated_at = ?
           WHERE status = 'processing' AND claimed_at < ?""",
        (utcnow(), cutoff),
    )
    return cur.rowcount


def recover_dead(conn: sqlite3.Connection, host: str) -> int:
    """Re-queue clips claimed by worker processes on this host that no longer exist
    (claimed_by is "host:pid"), without waiting for the stale-claim timeout."""
    n = 0
    for row in conn.execute("SELECT id, claimed_by FROM transmissions "
                            "WHERE status = 'processing' AND claimed_by LIKE ?", (f"{host}:%",)):
        try:
            os.kill(int(row["claimed_by"].rsplit(":", 1)[1]), 0)
            continue  # still running
        except ProcessLookupError:
            pass
        except (PermissionError, ValueError):
            continue
        conn.execute("""UPDATE transmissions SET status = 'queued', claimed_at = NULL,
                            claimed_by = NULL, error = 'recovered from dead worker', updated_at = ?
                        WHERE id = ? AND status = 'processing'""", (utcnow(), row["id"]))
        n += 1
    return n


def release_mine(conn: sqlite3.Connection, worker_id: str) -> int:
    """On clean shutdown: put anything this worker still holds back in the queue."""
    cur = conn.execute(
        """UPDATE transmissions SET status = 'queued', claimed_at = NULL, claimed_by = NULL,
               attempts = MAX(attempts - 1, 0), updated_at = ?
           WHERE status = 'processing' AND claimed_by = ?""",
        (utcnow(), worker_id),
    )
    return cur.rowcount


def stats(conn: sqlite3.Connection, window_hours: float = 1.0) -> dict:
    # "+status" keeps SQLite on the transcribed_at index: the last hour, not every done row
    by_status = dict(conn.execute(
        "SELECT status, COUNT(*) FROM transmissions GROUP BY status").fetchall())
    since = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat(timespec="milliseconds")
    rows = conn.execute(
        """SELECT ended_at, transcribed_at, stt_ms, duration_ms, flags, stt_backend, no_speech
           FROM transmissions WHERE +status = 'done' AND transcribed_at >= ?""",
        (since,),
    ).fetchall()
    lat = sorted(
        (datetime.fromisoformat(r["transcribed_at"]) - datetime.fromisoformat(r["ended_at"]))
        .total_seconds() for r in rows)
    rtf = [r["stt_ms"] / r["duration_ms"] for r in rows if r["duration_ms"]]
    flagged = sum(1 for r in rows if r["flags"] and json.loads(r["flags"]))
    oldest = conn.execute(
        "SELECT MIN(ended_at) FROM transmissions WHERE status = 'queued'").fetchone()[0]

    def pct(p: float) -> float | None:
        return round(lat[min(len(lat) - 1, int(p * len(lat)))], 1) if lat else None

    return {
        "by_status": by_status,
        "queued": by_status.get("queued", 0),
        "oldest_queued_age_s": (
            round((datetime.now(UTC) - datetime.fromisoformat(oldest)).total_seconds(), 1)
            if oldest else None),
        "done_last_window": len(rows),
        "window_hours": window_hours,
        "latency_s_p50": pct(0.5),
        "latency_s_p95": pct(0.95),
        "rtf_mean": round(sum(rtf) / len(rtf), 2) if rtf else None,
        "flagged_share": round(flagged / len(rows), 2) if rows else None,
        "no_speech_share": round(sum(r["no_speech"] or 0 for r in rows) / len(rows), 2)
        if rows else None,
        "backends": dict(conn.execute(
            """SELECT stt_backend, COUNT(*) FROM transmissions
               WHERE +status = 'done' AND transcribed_at >= ? GROUP BY stt_backend""",
            (since,)).fetchall()),
    }
