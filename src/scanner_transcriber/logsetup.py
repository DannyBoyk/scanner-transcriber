"""Logging for each process: console plus a small rotating file per component under
data/logs/ (capture.log, worker.log, web.log), which the Admin page can show."""
from __future__ import annotations

import logging
import logging.handlers
import re
from collections import deque
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
COMPONENTS = ("capture", "worker", "web")
MAX_BYTES, BACKUPS = 1_000_000, 3
_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} [\d:,]+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) ")


def log_path(data_dir: Path, component: str) -> Path:
    return Path(data_dir) / "logs" / f"{component}.log"


def setup(component: str, data_dir: Path, level: int = logging.INFO, *, console: bool = True) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(FORMAT)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)
    path = log_path(data_dir, component)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS,
                                                  encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        root.warning("file logging to %s unavailable: %s", path, e)


def tail(data_dir: Path, component: str, lines: int = 300, min_level: str = "INFO") -> list[dict]:
    """Last `lines` log records (multi-line tracebacks kept with their record), newest last."""
    order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    floor = order.index(min_level) if min_level in order else 1
    path = log_path(data_dir, component)
    files = [p for p in (Path(f"{path}.1"), path) if p.exists()]
    buf: deque[str] = deque(maxlen=lines * 4)
    for p in files:
        with p.open(encoding="utf-8", errors="replace") as f:
            buf.extend(f)
    records: list[dict] = []
    for raw in buf:
        m = _LINE.match(raw)
        if m:
            records.append({"level": m.group(1), "text": raw.rstrip("\n")})
        elif records:
            records[-1]["text"] += "\n" + raw.rstrip("\n")
    return [r for r in records if order.index(r["level"]) >= floor][-lines:]
