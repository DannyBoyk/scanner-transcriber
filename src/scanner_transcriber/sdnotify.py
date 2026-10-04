"""Minimal systemd notify protocol (stdlib only): READY=1, WATCHDOG=1, STOPPING=1.

A no-op unless the process runs under systemd with Type=notify (NOTIFY_SOCKET is set)."""
from __future__ import annotations

import logging
import os
import socket

log = logging.getLogger(__name__)


def notify(message: str) -> bool:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):            # abstract namespace socket
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.connect(addr)
            s.sendall(message.encode())
        return True
    except OSError as e:
        log.warning("systemd notify failed: %s", e)
        return False


def watchdog_enabled() -> bool:
    return bool(os.environ.get("WATCHDOG_USEC"))
