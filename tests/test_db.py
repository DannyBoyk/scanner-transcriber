import json
from datetime import timedelta

import numpy as np
import pytest
from segsim import T0

from scanner_transcriber import db
from scanner_transcriber.clipstore import ClipStore
from scanner_transcriber.models import Clip, ScannerStatus


def status(tgid="1101", **kw):
    fields = {"system": "Tri-County", "department": "County Sheriff",
              "channel": "Dispatch", "tgid": tgid, "mute": "Unmute", **kw}
    return ScannerStatus.from_summary(fields, mono=1.0, wall=T0, sample=0, raw_xml="<x/>")


def clip(t=0.0, **st_kw):
    return Clip(start_sample=0, end_sample=8000, pcm=np.ones(8000, np.int16),
                started_at=T0 + timedelta(seconds=t), status=status(**st_kw),
                unit_ids=["UID:1", "UID:2"], status_changes=[{"sample": 0}])


def test_migrations_apply_once(tmp_path):
    from importlib import resources
    n = sum(1 for f in resources.files("scanner_transcriber.migrations").iterdir()
            if f.name.endswith(".sql"))
    c = db.connect(tmp_path / "t.db")
    assert db.migrate(c) == n
    c.close()
    c = db.connect(tmp_path / "t.db")  # reopening doesn't re-apply
    assert c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == n
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_save_writes_file_and_queued_row(conn, tmp_path):
    store = ClipStore(conn, tmp_path, "sds200-1")
    tx = store.save(clip())
    row = conn.execute("SELECT * FROM transmissions WHERE id = ?", (tx,)).fetchone()
    assert row["status"] == "queued"
    local = T0.astimezone()
    assert row["audio_path"] == f"clips/{local:%Y/%m/%d}/{local:%H%M%S}_{tx}.wav"
    assert (tmp_path / row["audio_path"]).stat().st_size == 44 + 16000
    assert row["channel_key"] == "sys:Tri-County|tg:1101"
    assert json.loads(row["unit_ids"]) == ["UID:1", "UID:2"] and row["unit_id"] == "UID:1"
    assert row["duration_ms"] == 1000 and row["raw_status"] == "<x/>"


def test_channel_upsert_tracks_last_heard(conn, tmp_path):
    store = ClipStore(conn, tmp_path, "sds200-1")
    store.save(clip(0))
    store.save(clip(60))
    store.save(clip(30))  # out of order must not move last_heard backwards
    ch = conn.execute("SELECT * FROM channels").fetchall()
    assert len(ch) == 1
    assert ch[0]["first_heard_at"].startswith("2026-09-30T12:00:00")
    assert ch[0]["last_heard_at"].startswith("2026-09-30T12:01:00")


def test_channel_priority_copied_to_row(conn, tmp_path):
    store = ClipStore(conn, tmp_path, "sds200-1")
    store.save(clip())
    conn.execute("UPDATE channels SET priority = 5")
    tx = store.save(clip(5))
    assert conn.execute("SELECT priority FROM transmissions WHERE id=?", (tx,)).fetchone()[0] == 5


def test_fts_tracks_insert_update_delete(conn, tmp_path):
    store = ClipStore(conn, tmp_path, "sds200-1")
    tx = store.save(clip())

    def search(q):
        return [r[0] for r in conn.execute(
            "SELECT rowid FROM transmissions_fts WHERE transmissions_fts MATCH ?", (q,))]

    assert search("sheriff") == [tx]
    conn.execute("UPDATE transmissions SET transcript = 'traffic stop Mill Road' WHERE id=?",
                 (tx,))
    assert search("road") == [tx]
    conn.execute("UPDATE transmissions SET transcript = 'clear' WHERE id=?", (tx,))
    assert search("road") == []
    conn.execute("DELETE FROM transmissions WHERE id=?", (tx,))
    assert search("sheriff") == []


def test_failed_file_write_rolls_back_row(conn, tmp_path, monkeypatch):
    store = ClipStore(conn, tmp_path, "sds200-1")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("scanner_transcriber.clipstore.write_wav", boom)
    with pytest.raises(OSError):
        store.save(clip())
    assert conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 0
    assert not list((tmp_path / "clips").rglob("*.wav"))


def test_conventional_channel_key():
    st = status(tgid=None, frequency_hz=154_445_000)
    assert db.channel_key(st) == \
        "sys:Tri-County|ch:Dispatch|f:154445000"


def test_ignored_channel_clip_is_skipped(conn, tmp_path):
    store = ClipStore(conn, tmp_path, "sds200-1")
    store.save(clip())
    conn.execute("UPDATE channels SET ignored = 1")
    tx = store.save(clip(5))
    row = conn.execute("SELECT status, error, audio_path FROM transmissions WHERE id=?",
                       (tx,)).fetchone()
    assert row["status"] == "skipped" and row["error"] == "ignored channel"
    assert row["audio_path"]  # audio is still kept (soft ignore = record, don't transcribe)


def test_fts_indexes_alt_transcript(conn, tmp_path):
    tx = ClipStore(conn, tmp_path, "sds200-1").save(clip())
    conn.execute("UPDATE transmissions SET alt_transcript = 'engine twelve' WHERE id=?", (tx,))
    assert [r[0] for r in conn.execute(
        "SELECT rowid FROM transmissions_fts WHERE transmissions_fts MATCH 'twelve'")] == [tx]
