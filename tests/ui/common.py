"""Shared bits for the browser checks (run by run_all.sh): headless Firefox, PASS/FAIL
reporting with a failing exit code, waiting for a condition, and inserting transmissions."""
from __future__ import annotations

import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service

BASE = "http://127.0.0.1:8099"
WAIT_S = 6            # default for wait(); a script may raise it
_ok = True


def check(name: str, cond) -> bool:
    global _ok
    print(("PASS " if cond else "FAIL ") + name, flush=True)
    _ok &= bool(cond)
    return bool(cond)


def wait(fn, t: float | None = None, step: float = 0.2) -> bool:
    """True as soon as fn() is truthy (exceptions count as not yet), False after t seconds."""
    end = time.time() + (WAIT_S if t is None else t)
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def browser(width: int = 1280, height: int = 900, autoplay: bool = False, dark_device: bool = False,
            scale: float = 1):
    o = Options()
    o.add_argument("-headless")
    o.binary_location = "/snap/firefox/current/usr/lib/firefox/firefox"
    # what the "device" prefers: pages see prefers-color-scheme dark (0) or light (1)
    o.set_preference("layout.css.prefers-color-scheme.content-override", 0 if dark_device else 1)
    if autoplay:
        o.set_preference("media.autoplay.default", 0)
    if scale != 1:   # device pixel ratio: sharper screenshots
        o.set_preference("layout.css.devPixelsPerPx", str(scale))
    d = webdriver.Firefox(options=o, service=Service("/snap/bin/geckodriver"))
    d.set_window_size(width, height)
    return d


def finish() -> None:
    """Last line of every check: the summary, and exit 1 if anything failed."""
    print("ALL OK" if _ok else "SOME FAILED", flush=True)
    sys.exit(0 if _ok else 1)


def now(offset_s: float = 0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset_s)).isoformat(timespec="milliseconds")


def connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, isolation_level=None)


def insert_tx(conn: sqlite3.Connection, started: datetime, dur_s: float = 1.0, **cols) -> int:
    """Insert a transmission row; defaults to a finished County Sheriff Dispatch clip on
    TG 1101. Any other column can be given by name."""
    row = {"started_at": started.isoformat(timespec="milliseconds"),
           "ended_at": (started + timedelta(seconds=dur_s)).isoformat(timespec="milliseconds"),
           "duration_ms": int(dur_s * 1000), "system": "Sys", "department": "County Sheriff",
           "channel": "Dispatch", "tgid": "1101", "unit_id": "UID:1", "status": "done",
           "audio_format": "wav", "created_at": now(), "updated_at": now(), **cols}
    sql = (f"INSERT INTO transmissions ({', '.join(row)}) "
           f"VALUES ({', '.join('?' * len(row))})")
    return conn.execute(sql, list(row.values())).lastrowid
