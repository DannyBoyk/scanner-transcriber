"""End to end: synthetic `record` session → ReplaySource → segmenter → DB + WAV files."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime

from segsim import RATE, T0, Sim

from scanner_transcriber import db
from scanner_transcriber.app import run_pipeline
from scanner_transcriber.audio import write_wav
from scanner_transcriber.config import Config, StorageConfig
from scanner_transcriber.source.replay import ReplaySource


def write_session(d, sim: Sim, mono0=5000.0):
    """Lay a Sim out exactly as `record` would (audio, anchors, status, session)."""
    d.mkdir()
    write_wav(d / "audio.wav", sim.audio())
    with (d / "anchors.jsonl").open("w") as f:
        for s in range(0, int(sim.seconds), 1):
            f.write(json.dumps({"mono": mono0 + s, "sample": s * RATE}) + "\n")
    with (d / "status.jsonl").open("w") as f:
        for t, fields in sim.statuses:
            wall = datetime.fromtimestamp(T0.timestamp() + t, T0.tzinfo).isoformat()
            f.write(json.dumps({"mono": mono0 + t, "wall": wall,
                                "status": fields, "xml": "<ScannerInfo/>"}) + "\n")
    (d / "session.json").write_text(json.dumps({"started_wall": T0.isoformat(),
                                                "started_mono": mono0}))


def test_replay_session_to_db(tmp_path):
    sim = (Sim(20).status(0.2, "Mute")
           .ptt(1.0, 3.0, "1101", unit="UID:1")
           .ptt(4.0, 5.5, "1101", unit="UID:2")
           .ptt(8.0, 8.1, "1201")                      # click → dropped
           .ptt(10.0, 14.0, "1201", unit="UID:9"))
    write_session(tmp_path / "session", sim)
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path / "data"))

    stats = asyncio.run(run_pipeline(cfg, ReplaySource(tmp_path / "session")))

    assert stats.clips == 3
    conn = db.connect(cfg.storage.db_path)
    rows = conn.execute("SELECT * FROM transmissions ORDER BY started_at").fetchall()
    assert [r["tgid"] for r in rows] == ["1101", "1101", "1201"]
    assert [r["unit_id"] for r in rows] == ["UID:1", "UID:2", "UID:9"]
    assert all(r["status"] == "queued" for r in rows)
    assert rows[0]["started_at"].startswith("2026-09-30T12:00:00.9")
    for r in rows:
        assert (cfg.storage.data_dir / r["audio_path"]).exists()
    assert conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 2


def _two_ptt_session(tmp_path):
    sim = (Sim(20).status(0.2, "Mute").ptt(1.0, 3.0, "1101", unit="UID:1")
           .ptt(10.0, 14.0, "1201", unit="UID:9"))
    write_session(tmp_path / "session", sim)
    return replace(Config(), storage=StorageConfig(data_dir=tmp_path / "data"))


def test_a_failed_save_is_retried_and_capture_goes_on(tmp_path, monkeypatch):
    import sqlite3

    from scanner_transcriber import app
    from scanner_transcriber.clipstore import ClipStore
    cfg = _two_ptt_session(tmp_path)
    monkeypatch.setattr(app, "SAVE_RETRY_S", (0.01, 0.01, 0.01))
    real, calls = ClipStore.save, []

    def flaky(self, clip):
        calls.append(1)
        if len(calls) <= 2:
            raise sqlite3.OperationalError("database is locked")
        return real(self, clip)

    monkeypatch.setattr(ClipStore, "save", flaky)
    stats = asyncio.run(run_pipeline(cfg, ReplaySource(tmp_path / "session")))
    assert stats.clips == 2 and stats.spilled == 0
    conn = db.connect(cfg.storage.db_path)
    assert [r[0] for r in conn.execute("SELECT tgid FROM transmissions ORDER BY id")] == ["1101", "1201"]


def test_a_save_that_keeps_failing_spills_the_audio_and_capture_goes_on(tmp_path, monkeypatch):
    from scanner_transcriber import app
    from scanner_transcriber.clipstore import ClipStore
    cfg = _two_ptt_session(tmp_path)
    monkeypatch.setattr(app, "SAVE_RETRY_S", (0.01,))

    def broken(self, clip):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ClipStore, "save", broken)
    stats = asyncio.run(run_pipeline(cfg, ReplaySource(tmp_path / "session")))
    assert stats.clips == 0 and stats.spilled == 2
    spilled = sorted(p.name for p in (cfg.storage.data_dir / "spill").iterdir())
    assert len(spilled) == 2 and "_TG1101" in spilled[0] and "_TG1201" in spilled[1]
    conn = db.connect(cfg.storage.db_path)
    ev = conn.execute("SELECT message FROM system_events WHERE kind = 'clip_not_saved'").fetchall()
    assert len(ev) == 2 and "No space left" in ev[0][0]
