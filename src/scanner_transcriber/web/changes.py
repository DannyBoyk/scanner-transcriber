"""Change feed for Server-Sent Events: which transmission rows changed since a cursor.

The capture process and the worker both write `transmissions`, and `updated_at` is taken
before a transaction commits, so a row can become visible slightly *after* a newer timestamp
has already been seen. Each poll therefore re-reads an overlap window behind the cursor and
skips (id, updated_at) pairs already sent. Resuming from an event id ("<updated_at>|<id>")
replays that window, which the browser applies idempotently (rows are replaced by id)."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .feed import COLUMNS

OVERLAP_S = 2.0
MAX_BATCH = 500


def _shift(ts: str, seconds: float) -> str:
    return (datetime.fromisoformat(ts) + timedelta(seconds=seconds)).isoformat(
        timespec="milliseconds")


def event_id(row: sqlite3.Row) -> str:
    return f"{row['updated_at']}|{row['id']}"


def cursor_from_event_id(value: str) -> str:
    ts = value.split("|", 1)[0]
    datetime.fromisoformat(ts)  # validate
    return ts


class ChangeFeed:
    """Delivers each (id, updated_at) once. Two reads per poll:

    * forward: rows after the position (updated_at, id) of the last row delivered, in exact
      keyset order, so any number of rows sharing one timestamp (a re-transcribe batch
      stamps up to 2000 at once) pages through without stalling;
    * late: rows that committed with a timestamp inside the overlap window *behind* the
      position. Found with a cheap (id, updated_at) scan of the window; only unseen ones are
      fetched in full."""

    def __init__(self, cursor: str, tgs: list[str] | None = None) -> None:
        self.cursor = cursor           # newest updated_at delivered so far
        self.after_id = 0              # ... and the highest id delivered at that timestamp
        self.tgs = list(tgs or [])
        self._sent: dict[int, str] = {}  # id -> updated_at already seen (within window)

    def _select(self, where: str, args: list, limit: int) -> list[sqlite3.Row]:
        sql = (f"SELECT {COLUMNS} FROM transmissions t "
               f"LEFT JOIN channels c ON c.key = t.channel_key WHERE {where}")
        if self.tgs:
            sql += f" AND t.tgid IN ({','.join('?' * len(self.tgs))})"
            args = [*args, *self.tgs]
        else:
            sql += " AND COALESCE(c.hidden, 0) = 0"   # hidden talkgroups: only when picked
        return self._conn.execute(sql + " ORDER BY t.updated_at, t.id LIMIT ?",
                                  (*args, limit)).fetchall()

    def poll(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        self._conn = conn
        out: list[sqlite3.Row] = []
        # late commits inside the window behind the position
        floor = _shift(self.cursor, -OVERLAP_S)
        unseen = [(i, u) for i, u in conn.execute(
            "SELECT id, updated_at FROM transmissions WHERE updated_at >= ? "
            "AND (updated_at, id) <= (?, ?)", (floor, self.cursor, self.after_id))
            if self._sent.get(i) != u][:MAX_BATCH]
        for k in range(0, len(unseen), 500):
            part = unseen[k:k + 500]
            out += self._select(f"t.id IN ({','.join('?' * len(part))})",
                                [i for i, _ in part], len(part))
            self._sent.update(part)          # filtered-out rows count as seen too
        # forward from the position
        rows = self._select("(t.updated_at, t.id) > (?, ?)", [self.cursor, self.after_id],
                            MAX_BATCH)
        for r in rows:
            self._sent[r["id"]] = r["updated_at"]
            out.append(r)
        if rows:
            self.cursor, self.after_id = rows[-1]["updated_at"], rows[-1]["id"]
        floor = _shift(self.cursor, -OVERLAP_S)
        self._sent = {k: v for k, v in self._sent.items() if v >= floor}
        return out
