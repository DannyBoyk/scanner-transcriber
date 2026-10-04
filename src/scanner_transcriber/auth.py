"""Admin password and browser sessions (stdlib only).

The password is stored as a scrypt hash in the `settings` table (set on the NUC with
`python -m scanner_transcriber set-password`). A session is a signed cookie
"<expiry>.<hmac>" keyed by a random secret that is rotated whenever the password changes, so
changing the password logs every browser out. No password set = admin disabled (fail closed)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
import time

from .db import utcnow

COOKIE = "st_admin"
SESSION_DAYS = 30
_N, _R, _P = 2 ** 14, 8, 1


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                     "updated_at = excluded.updated_at", (key, value, utcnow()))


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(h)}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n),
                             r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(got, base64.b64decode(h))
    except (ValueError, TypeError):
        return False


def set_password(conn: sqlite3.Connection, password: str | None) -> None:
    """Set (or with None, clear) the admin password. Always rotates the session secret."""
    set_setting(conn, "admin_password", hash_password(password) if password else None)
    set_setting(conn, "session_secret", secrets.token_hex(32))


def enabled(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "admin_password") is not None


def verify(conn: sqlite3.Connection, password: str) -> bool:
    stored = get_setting(conn, "admin_password")
    return bool(stored) and check_password(password, stored)


def _secret(conn: sqlite3.Connection) -> bytes:
    s = get_setting(conn, "session_secret")
    if s is None:
        s = secrets.token_hex(32)
        set_setting(conn, "session_secret", s)
    return bytes.fromhex(s)


def make_session(conn: sqlite3.Connection, now: float | None = None) -> tuple[str, int]:
    expires = int((now or time.time()) + SESSION_DAYS * 86400)
    sig = hmac.new(_secret(conn), str(expires).encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{sig}", SESSION_DAYS * 86400


def valid_session(conn: sqlite3.Connection, value: str | None, now: float | None = None) -> bool:
    if not value or not enabled(conn):
        return False
    try:
        expires, sig = value.split(".", 1)
        if int(expires) < (now or time.time()):
            return False
    except ValueError:
        return False
    want = hmac.new(_secret(conn), expires.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, want)


class LoginLimiter:
    """Exponential delay per client after failed logins (1, 2, 4 … 60 s); reset on success."""

    def __init__(self) -> None:
        self._fails: dict[str, tuple[int, float]] = {}

    def wait_s(self, client: str, now: float | None = None) -> float:
        n, until = self._fails.get(client, (0, 0.0))
        return max(0.0, until - (now or time.time()))

    def failed(self, client: str, now: float | None = None) -> None:
        n, _ = self._fails.get(client, (0, 0.0))
        n += 1
        self._fails[client] = (n, (now or time.time()) + min(60.0, 2.0 ** (n - 1)))

    def ok(self, client: str) -> None:
        self._fails.pop(client, None)
