"""Re-transcribe past clips (e.g. after a prompt change) at low priority, keeping the old text
so the Admin page can show old vs new."""
from __future__ import annotations

import json
import sqlite3

from . import promptstore
from .db import utcnow

MAX_BATCH = 2000
PRIORITY = -1          # live clips are 0 and are always claimed first


def select_where(since: str | None, until: str | None, tgids: list[str],
                 include_no_speech: bool) -> tuple[str, list]:
    clauses = ["status = 'done'", "audio_path IS NOT NULL"]
    args: list = []
    if since:
        clauses.append("started_at >= ?"); args.append(since)
    if until:
        clauses.append("started_at < ?"); args.append(until)
    if tgids:
        clauses.append(f"tgid IN ({','.join('?' * len(tgids))})"); args += tgids
    if not include_no_speech:
        clauses.append("COALESCE(no_speech, 0) = 0")
    return " AND ".join(clauses), args


def preview(conn: sqlite3.Connection, since, until, tgids, include_no_speech) -> dict:
    where, args = select_where(since, until, tgids, include_no_speech)
    n = conn.execute(f"SELECT COUNT(*) FROM transmissions WHERE {where}", args).fetchone()[0]
    avg = conn.execute("SELECT AVG(stt_ms) FROM (SELECT stt_ms FROM transmissions WHERE stt_ms "
                       "IS NOT NULL AND status = 'done' ORDER BY id DESC LIMIT 200)").fetchone()[0]
    per = (avg or 4700) / 1000
    return {"count": n, "capped": min(n, MAX_BATCH), "over_cap": n > MAX_BATCH,
            "seconds": min(n, MAX_BATCH) * per}


def start(conn: sqlite3.Connection, since, until, tgids, include_no_speech) -> tuple[int, int]:
    """Create a batch and queue its clips (oldest first, capped). Returns (batch id, count)."""
    where, args = select_where(since, until, tgids, include_no_speech)
    ids = [r[0] for r in conn.execute(
        f"SELECT id FROM transmissions WHERE {where} ORDER BY started_at LIMIT ?",
        (*args, MAX_BATCH))]
    pv, _ = promptstore.active(conn, "prompt")
    bid = conn.execute(
        "INSERT INTO retranscribe_batches (created_at, since, until, tgids, prompt_version, total) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (utcnow(), since, until, json.dumps(tgids) if tgids else None, pv, len(ids))).lastrowid
    now = utcnow()
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        conn.execute(
            f"""UPDATE transmissions SET prev_transcript = transcript,
                   prev_prompt_version = prompt_version, batch_id = ?, status = 'queued',
                   priority = ?, attempts = 0, error = NULL, updated_at = ?
                WHERE id IN ({','.join('?' * len(part))})""", (bid, PRIORITY, now, *part))
    return bid, len(ids)


def batches(conn: sqlite3.Connection, limit: int = 6) -> list[dict]:
    out = []
    for b in conn.execute("SELECT * FROM retranscribe_batches ORDER BY id DESC LIMIT ?", (limit,)):
        d = dict(b)
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM transmissions WHERE batch_id = ? "
                                   "GROUP BY status", (b["id"],)).fetchall())
        d["done"] = counts.get("done", 0) + counts.get("failed", 0)
        d["changed"] = conn.execute(
            "SELECT COUNT(*) FROM transmissions WHERE batch_id = ? AND status = 'done' "
            "AND COALESCE(transcript, '') != COALESCE(prev_transcript, '')", (b["id"],)).fetchone()[0]
        d["finished"] = d["done"] >= d["total"]
        out.append(d)
    return out


def batch_rows(conn: sqlite3.Connection, batch_id: int, changed_only: bool, limit: int = 300):
    sql = """SELECT t.id, t.started_at, t.tgid, t.channel, t.department, t.status, t.transcript,
                    t.prev_transcript, t.prompt_version, t.prev_prompt_version, c.alias, c.color
             FROM transmissions t LEFT JOIN channels c ON c.key = t.channel_key
             WHERE t.batch_id = ?"""
    if changed_only:
        sql += (" AND t.status = 'done' AND COALESCE(t.transcript, '') != "
                "COALESCE(t.prev_transcript, '')")
    return conn.execute(sql + " ORDER BY t.started_at LIMIT ?", (batch_id, limit)).fetchall()
