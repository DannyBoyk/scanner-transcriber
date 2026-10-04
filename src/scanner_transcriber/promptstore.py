"""Versioned Whisper prompt and substitution map, stored in the database.

The database is the source of truth (edited on the Admin page). On first start, whatever is in
`config.toml` ([stt] prompt / [stt.substitutions]) is imported once as version 1; after that
the config values are ignored (with a warning if they differ). A built-in default covers a
fresh install with nothing configured."""
from __future__ import annotations

import json
import logging
import sqlite3

from .config import Config
from .db import utcnow

log = logging.getLogger(__name__)

KINDS = ("prompt", "substitutions")

# An example in the shape that works (the limit is 223 Whisper tokens): replace the place,
# road and unit names with your own on the Admin page.
DEFAULT_PROMPT = (
    "Northfield County, Lakeside County. Ashford, Brookfield, Carver, Dunmore, Easton, Fairview, "
    "The Glen. Route 12, Lakeside Highway, Ridge Highway, Hillcrest Avenue, "
    "Quarry Road. Valley Inn, County Hospital. Medic 1, Medic 2, Medic 3, Engine 1, "
    "Engine 2, Tanker 1, Battalion. En route, on scene, traffic stop, summons, status check, "
    "radio check, go ahead, copy, clear, direct, I'm direct, back at station, patient report, "
    "for an illness. Alpha, Bravo, Charlie, Delta, Echo, Foxtrot, Golf, Hotel, India, Juliet, "
    "Kilo, Lima, Mike, November, Oscar, Papa, Quebec, Romeo, Sierra, Tango, Uniform, Victor, "
    "Whiskey, X-ray, Yankee, Zulu.")


def _encode(kind: str, value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if kind == "substitutions" \
        else str(value)


def decode(kind: str, body: str):
    return json.loads(body) if kind == "substitutions" else body


def active(conn: sqlite3.Connection, kind: str) -> tuple[int | None, object]:
    """(version id, value) of the active version; (None, default) if nothing stored yet."""
    row = conn.execute(
        "SELECT v.id, v.body FROM active_versions a JOIN prompt_versions v ON v.id = a.version_id "
        "WHERE a.kind = ?", (kind,)).fetchone()
    if row is None:
        return None, (DEFAULT_PROMPT if kind == "prompt" else {})
    return row[0], decode(kind, row[1])


def active_ids(conn: sqlite3.Connection) -> dict[str, int | None]:
    ids = dict(conn.execute("SELECT kind, version_id FROM active_versions").fetchall())
    return {k: ids.get(k) for k in KINDS}


def save(conn: sqlite3.Connection, kind: str, value, note: str | None = None) -> int:
    """Store a new version and make it active. Returns its id."""
    if kind not in KINDS:
        raise ValueError(kind)
    now = utcnow()
    vid = conn.execute(
        "INSERT INTO prompt_versions (kind, body, note, created_at, activated_at) "
        "VALUES (?, ?, ?, ?, ?)", (kind, _encode(kind, value), note, now, now)).lastrowid
    conn.execute("INSERT INTO active_versions (kind, version_id) VALUES (?, ?) "
                 "ON CONFLICT(kind) DO UPDATE SET version_id = excluded.version_id", (kind, vid))
    return vid


def activate(conn: sqlite3.Connection, version_id: int) -> str:
    row = conn.execute("SELECT kind FROM prompt_versions WHERE id = ?", (version_id,)).fetchone()
    if row is None:
        raise KeyError(version_id)
    conn.execute("UPDATE prompt_versions SET activated_at = ? WHERE id = ?", (utcnow(), version_id))
    conn.execute("INSERT INTO active_versions (kind, version_id) VALUES (?, ?) "
                 "ON CONFLICT(kind) DO UPDATE SET version_id = excluded.version_id",
                 (row[0], version_id))
    return row[0]


def history(conn: sqlite3.Connection, kind: str) -> list[dict]:
    act = active_ids(conn).get(kind)
    rows = conn.execute(
        """SELECT v.*, (SELECT COUNT(*) FROM transmissions t WHERE t.{col} = v.id) AS used
           FROM prompt_versions v WHERE v.kind = ? ORDER BY v.id DESC""".format(
            col="prompt_version" if kind == "prompt" else "subs_version"), (kind,)).fetchall()
    return [dict(r) | {"value": decode(kind, r["body"]), "active": r["id"] == act} for r in rows]


def ensure_seeded(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    """Import config (or defaults) once into an empty store. Returns warnings to log/show."""
    warnings = []
    seeds = {"prompt": (cfg.stt.prompt, DEFAULT_PROMPT),
             "substitutions": (dict(cfg.stt.substitutions), {})}
    for kind, (from_config, default) in seeds.items():
        vid, current = active(conn, kind)
        if vid is None:
            if from_config:
                save(conn, kind, from_config, note="imported from config.toml")
            else:
                save(conn, kind, default, note="built-in default")
        elif from_config and from_config != current:
            warnings.append(
                f"config.toml has a [stt] {kind} that differs from the active one in the "
                f"database; it is ignored. Edit {kind} on the Admin page and delete it from "
                "config.toml.")
    return warnings
