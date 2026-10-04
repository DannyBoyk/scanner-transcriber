"""Transmission list queries for the live feed / jump-to view, and row presentation.

Paging is keyset-based on (started_at, id), so "load older" / "newer" stay exact while new
rows keep arriving. Cursors are URL-safe strings: "<epoch_ms>_<id>"."""
from __future__ import annotations

import json
import sqlite3
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo

COLUMNS = """t.id, t.started_at, t.ended_at, t.duration_ms, t.system, t.department, t.channel,
             t.tgid, t.unit_id, t.unit_ids, t.status, t.transcript, t.flags, t.no_speech,
             t.audio_path, t.continued, t.status_missing, t.channel_key, t.updated_at,
             c.alias, c.color"""

# Ten distinguishable hues for talkgroup colours (no pure red: that means "problem" here).
PALETTE = (212, 150, 28, 275, 185, 325, 48, 248, 95, 15)

GROUP_GAP_S = 10  # consecutive rows on one talkgroup closer than this are drawn as one exchange

FLAG_TEXT = {
    "hallucination": "Model output looked like filler (e.g. \"Thank you.\"); checked with a second model",
    "prompt_leak": "Model repeated words from its vocabulary hint; re-run without it",
    "repeat": "Repeated words were collapsed",
    "degraded": "Transcribed by the CPU fallback model (lower accuracy)",
}


def encode_cursor(started_at: str, tx_id: int) -> str:
    ms = int(datetime.fromisoformat(started_at).timestamp() * 1000)
    return f"{ms}_{tx_id}"


def decode_cursor(cursor: str) -> tuple[str, int]:
    ms, tx_id = cursor.split("_", 1)
    ts = datetime.fromtimestamp(int(ms) / 1000, UTC).isoformat(timespec="milliseconds")
    return ts, int(tx_id)


def _where(tgs: list[str], hide_no_speech: bool) -> tuple[list[str], list]:
    where, args = [], []
    if tgs:
        where.append(f"t.tgid IN ({','.join('?' * len(tgs))})")
        args += tgs
    else:
        where.append("COALESCE(c.hidden, 0) = 0")  # hidden talkgroups: only when picked
    if hide_no_speech:
        where.append("COALESCE(t.no_speech, 0) = 0")
    return where, args


def _query(conn: sqlite3.Connection, where: list[str], args: list, order: str,
           limit: int) -> list[sqlite3.Row]:
    sql = f"SELECT {COLUMNS} FROM transmissions t LEFT JOIN channels c ON c.key = t.channel_key"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # started_at is ISO-8601 UTC with fixed width, so string order == time order
    sql += f" ORDER BY t.started_at {order}, t.id {order} LIMIT ?"
    return conn.execute(sql, (*args, limit)).fetchall()


def latest(conn, *, tgs=(), hide_no_speech=False, limit=50) -> list[sqlite3.Row]:
    where, args = _where(list(tgs), hide_no_speech)
    return _query(conn, where, args, "DESC", limit)


def older(conn, cursor: str, *, tgs=(), hide_no_speech=False, limit=50) -> list[sqlite3.Row]:
    ts, tx_id = decode_cursor(cursor)
    where, args = _where(list(tgs), hide_no_speech)
    where.append("(t.started_at, t.id) < (?, ?)")  # row value: walks the index, no sort
    args += [ts, tx_id]
    return _query(conn, where, args, "DESC", limit)


def newer(conn, cursor: str, *, tgs=(), hide_no_speech=False, limit=50) -> list[sqlite3.Row]:
    ts, tx_id = decode_cursor(cursor)
    where, args = _where(list(tgs), hide_no_speech)
    where.append("(t.started_at, t.id) > (?, ?)")
    args += [ts, tx_id]
    return list(reversed(_query(conn, where, args, "ASC", limit)))  # newest first


def around(conn, at: datetime, *, tgs=(), hide_no_speech=False,
           each_side=25) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """(rows at/after `at`, rows before `at`), both newest first."""
    ts = at.astimezone(UTC).isoformat(timespec="milliseconds")
    where, args = _where(list(tgs), hide_no_speech)
    after_rows = _query(conn, where + ["t.started_at >= ?"], args + [ts], "ASC", each_side)
    before_rows = _query(conn, where + ["t.started_at < ?"], args + [ts], "DESC", each_side)
    return list(reversed(after_rows)), before_rows


def time_bounds(conn) -> tuple[str | None, str | None]:
    # two subqueries: each is one index lookup (MIN and MAX in one SELECT scan the index)
    return conn.execute("SELECT (SELECT MIN(started_at) FROM transmissions), "
                        "(SELECT MAX(started_at) FROM transmissions)").fetchone()


def hue_for(color: int | None, tgid: str | None) -> int:
    """Talkgroup colour: the one stored when it was first heard; hash fallback if missing."""
    if color is not None:
        return PALETTE[color % len(PALETTE)]
    return PALETTE[zlib.crc32((tgid or "none").encode()) % len(PALETTE)]


def recent_talkgroups(conn, hours: float = 24) -> list[dict]:
    """Talkgroups heard recently, busiest first, with a label that's unambiguous among them
    ("Dispatch" twice → department added) and their colour."""
    since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    rows = conn.execute(
        """SELECT t.tgid, MAX(t.channel) AS channel, MAX(t.department) AS department,
                  MAX(c.alias) AS alias, MAX(c.color) AS color, MAX(c.hidden) AS hidden,
                  COUNT(*) AS n
           FROM transmissions t LEFT JOIN channels c ON c.key = t.channel_key
           WHERE t.started_at >= ? AND t.tgid IS NOT NULL
           GROUP BY t.tgid ORDER BY n DESC""", (since,)).fetchall()
    return _labelled(rows)


def known_talkgroups(conn, busy_days: float = 7) -> list[dict]:
    """Every talkgroup ever heard (from `channels`, so it stays fast with a year of rows),
    busiest in the last `busy_days` first, then most recently heard."""
    since = (datetime.now(UTC) - timedelta(days=busy_days)).isoformat()
    rows = conn.execute(
        """SELECT c.tgid, MAX(c.channel) AS channel, MAX(c.department) AS department,
                  MAX(c.alias) AS alias, MAX(c.color) AS color, MAX(c.hidden) AS hidden,
                  (SELECT COUNT(*) FROM transmissions t
                    WHERE t.tgid = c.tgid AND t.started_at >= ?) AS n,
                  MAX(c.last_heard_at) AS last_heard_at
           FROM channels c WHERE c.tgid IS NOT NULL
           GROUP BY c.tgid ORDER BY n DESC, last_heard_at DESC""", (since,)).fetchall()
    return _labelled(rows)


def _labelled(rows) -> list[dict]:
    out = [dict(r) | {"name": r["alias"] or r["channel"] or f"TG {r['tgid']}",
                      "hue": hue_for(r["color"], r["tgid"])} for r in rows]
    counts: dict[str, int] = {}
    for t in out:
        counts[t["name"]] = counts.get(t["name"], 0) + 1
    for t in out:
        t["qualifier"] = t["department"] if counts[t["name"]] > 1 and not t["alias"] else None
    return out


# ---- presentation ----------------------------------------------------------------------------

@dataclass
class Row:
    id: int
    started: datetime
    started_at: str
    ended_at: str
    duration_s: float
    tgid: str | None
    label: str
    department: str | None
    unit: str | None
    status: str
    transcript: str | None
    no_speech: bool
    flags: list[str]
    has_audio: bool
    cursor: str
    cont: bool = False          # same exchange as the row above it (newer): no header
    transcript_html: object = None  # search results: transcript with <mark>ed matches
    day_break: str | None = None  # date label to draw above this row

    color: int | None = None

    @property
    def day(self) -> str:
        return self.started.strftime("%A, %b %d")

    @property
    def hue(self) -> int:
        return hue_for(self.color, self.tgid)

    @property
    def pending(self) -> bool:
        return self.status in ("queued", "processing")

    @property
    def flag_notes(self) -> list[tuple[str, str]]:
        return [(f, FLAG_TEXT.get(f, f)) for f in self.flags]


def present(rows: list[sqlite3.Row], tz: tzinfo | None) -> list[Row]:
    """Rows are newest first. Marks exchanges (`cont`) and day changes (`day_break`)."""
    out = []
    for r in rows:
        started = datetime.fromisoformat(r["started_at"]).astimezone(tz)
        unit = (r["unit_id"] or "").removeprefix("UID:") or None
        out.append(Row(
            id=r["id"], started=started, started_at=r["started_at"], ended_at=r["ended_at"],
            duration_s=r["duration_ms"] / 1000, tgid=r["tgid"],
            label=r["alias"] or r["channel"] or (f"TG {r['tgid']}" if r["tgid"] else "Unknown"),
            department=r["department"], unit=unit, status=r["status"],
            transcript=r["transcript"], no_speech=bool(r["no_speech"]),
            flags=json.loads(r["flags"]) if r["flags"] else [],
            has_audio=bool(r["audio_path"]), cursor=encode_cursor(r["started_at"], r["id"]),
            color=r["color"]))
    for newer_row, older_row in zip(out, out[1:]):
        older_row.cont = same_exchange(newer_row, older_row, tz)
    return out


def same_exchange(newer_row: Row, older_row: Row, tz: tzinfo | None = None) -> bool:
    gap = newer_row.started - datetime.fromisoformat(older_row.ended_at).astimezone(tz)
    return (newer_row.tgid is not None and newer_row.tgid == older_row.tgid
            and gap.total_seconds() < GROUP_GAP_S)


def mark_days(rows: list[Row], above: date | None = None) -> list[Row]:
    """Set `day_break` where the local date changes (rows newest first). `above` is the local
    date of the row just above this batch on the page, if known."""
    for r in rows:
        if above is None or above != r.started.date():
            r.day_break = r.day
        above = r.started.date()
    return rows


def cursor_date(cursor: str, tz: tzinfo | None) -> date:
    return datetime.fromisoformat(decode_cursor(cursor)[0]).astimezone(tz).date()
