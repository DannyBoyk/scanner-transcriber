"""Component heartbeats (`component_state`) and the system event log (`system_events`)."""
from __future__ import annotations

import json
import os
import sqlite3

from .db import utcnow


def publish(conn: sqlite3.Connection, name: str, state: str, detail: dict) -> None:
    conn.execute(
        """INSERT INTO component_state (name, state, detail, pid, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET state = excluded.state, detail = excluded.detail,
               pid = excluded.pid, updated_at = excluded.updated_at""",
        (name, state, json.dumps(detail, default=str), os.getpid(), utcnow()),
    )


def event(conn: sqlite3.Connection, component: str, level: str, kind: str, message: str,
          detail: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO system_events (at, component, level, kind, message, detail) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (utcnow(), component, level, kind, message,
         json.dumps(detail, default=str) if detail else None),
    )


def states(conn: sqlite3.Connection) -> dict[str, dict]:
    out = {}
    for r in conn.execute("SELECT * FROM component_state"):
        out[r["name"]] = {"state": r["state"], "detail": json.loads(r["detail"]),
                          "pid": r["pid"], "updated_at": r["updated_at"]}
    return out


def recent_events(conn: sqlite3.Connection, limit: int = 50, component: str | None = None,
                  since: str | None = None) -> list[dict]:
    where, args = [], []
    if component:
        where.append("component = ?")
        args.append(component)
    if since:
        where.append("at >= ?")
        args.append(since)
    q = "SELECT * FROM system_events"
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY at DESC, id DESC LIMIT ?"
    return [dict(r) | {"detail": json.loads(r["detail"]) if r["detail"] else None}
            for r in conn.execute(q, (*args, limit))]
