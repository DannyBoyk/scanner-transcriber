"""Persists finished clips: WAV file on disk + queued DB row, atomically."""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from . import db
from .audio import write_wav
from .models import Clip

log = logging.getLogger(__name__)


class ClipStore:
    def __init__(self, conn: sqlite3.Connection, data_dir: Path, scanner_id: str) -> None:
        self.conn = conn
        self.data_dir = Path(data_dir)
        self.scanner_id = scanner_id

    def rel_path(self, clip: Clip, tx_id: int) -> str:
        """clips/YYYY/MM/DD/HHMMSS_<id>.wav in local time, so browsing by hand works
        ("around 9 pm last night"). The id keeps names unique (e.g. the repeated DST hour);
        the app itself always finds clips via the path stored in the row."""
        d = clip.started_at.astimezone()
        return f"clips/{d:%Y/%m/%d}/{d:%H%M%S}_{tx_id}.wav"

    def save(self, clip: Clip) -> int:
        """Row and file land together or not at all; the row is 'queued' for STT."""
        self.conn.execute("BEGIN IMMEDIATE")
        path: Path | None = None
        try:
            tx_id = db.insert_transmission(self.conn, clip, scanner_id=self.scanner_id)
            rel = self.rel_path(clip, tx_id)
            path = self.data_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".wav.tmp")
            write_wav(tmp, clip.pcm)
            os.replace(tmp, path)
            db.set_audio_path(self.conn, tx_id, rel)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            if path is not None:
                path.unlink(missing_ok=True)
                path.with_suffix(".wav.tmp").unlink(missing_ok=True)
            raise
        st = clip.status
        log.info("clip %d %.1fs tg=%s %s/%s units=%s", tx_id, clip.duration_ms / 1000,
                 st.tgid if st else None, st.department if st else None,
                 st.channel if st else None, ",".join(clip.unit_ids) or "-")
        return tx_id
