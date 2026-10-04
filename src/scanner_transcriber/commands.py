"""Commands from the Admin page to running processes, via the database (no OS privileges).

The web inserts a row; `run` (capture + worker supervisor) and the worker poll for rows
addressed to them, mark them taken, act, and record the result. Rows nobody takes within
EXPIRE_S are marked expired (e.g. capture wasn't running)."""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from . import statestore
from .db import utcnow

EXPIRE_S = 600
ACTIONS = {("capture", "reconnect"), ("capture", "reboot"), ("capture", "clean-start"),
           ("worker", "restart"), ("worker", "prompt-test")}


def post(conn: sqlite3.Connection, target: str, action: str, arg: dict | None = None) -> int:
    if (target, action) not in ACTIONS:
        raise ValueError(f"unknown command {target}/{action}")
    return conn.execute("INSERT INTO commands (target, action, arg, created_at) VALUES (?, ?, ?, ?)",
                        (target, action, json.dumps(arg) if arg else None, utcnow())).lastrowid


def take(conn: sqlite3.Connection, target: str, actions: set[str] | None = None) -> list[dict]:
    """Claim open commands for `target` (oldest first); expire stale ones."""
    cutoff = (datetime.now(UTC) - timedelta(seconds=EXPIRE_S)).isoformat()
    conn.execute("UPDATE commands SET done_at = ?, result = 'expired: nobody picked it up' "
                 "WHERE done_at IS NULL AND taken_at IS NULL AND created_at < ?", (utcnow(), cutoff))
    out = []
    for r in conn.execute("SELECT * FROM commands WHERE target = ? AND taken_at IS NULL "
                          "AND done_at IS NULL ORDER BY id", (target,)).fetchall():
        if actions and r["action"] not in actions:
            continue
        cur = conn.execute("UPDATE commands SET taken_at = ? WHERE id = ? AND taken_at IS NULL",
                           (utcnow(), r["id"]))
        if cur.rowcount:
            d = dict(r)
            d["arg"] = json.loads(d["arg"]) if d["arg"] else {}
            out.append(d)
    return out


def done(conn: sqlite3.Connection, cmd: dict, result: str, level: str = "info") -> None:
    conn.execute("UPDATE commands SET done_at = ?, result = ? WHERE id = ?", (utcnow(), result, cmd["id"]))
    statestore.event(conn, cmd["target"], level, f"command_{cmd['action']}",
                     f"{cmd['target'].capitalize()} {cmd['action']} (from Admin): {result}")


def recent(conn: sqlite3.Connection, limit: int = 8) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM commands WHERE action != 'prompt-test' ORDER BY id DESC LIMIT ?", (limit,))]
