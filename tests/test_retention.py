"""Retention: audio / raw data / rows / events / commands / prompt tests, dry run, safety."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np

from scanner_transcriber import commands, retention, statestore
from scanner_transcriber.audio import write_wav
from scanner_transcriber.config import Config, RetentionConfig, StorageConfig

NOW = datetime(2027, 6, 1, 12, 0, tzinfo=UTC)


def cfg(tmp_path, **ret):
    return replace(Config(), storage=StorageConfig(data_dir=tmp_path), retention=RetentionConfig(**ret))


def add(conn, d, days_ago, *, status="done", audio=True, transcript="clear", raw=True):
    started = NOW - timedelta(days=days_ago)
    # local folder date may differ from the UTC date: retention must not care
    rel = f"clips/{(started - timedelta(hours=5)):%Y/%m/%d}/x{days_ago}_{status}.wav"
    if audio:
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        write_wav(d / rel, np.ones(8000, np.int16))
    ts = started.isoformat(timespec="milliseconds")
    return conn.execute(
        """INSERT INTO transmissions (started_at, ended_at, duration_ms, tgid, status, transcript,
               audio_path, raw_status, status_changes, created_at, updated_at)
           VALUES (?, ?, 1000, '1101', ?, ?, ?, ?, ?, ?, ?)""",
        (ts, ts, status, transcript, rel if audio else None,
         "<ScannerInfo/>" if raw else None, "[]" if raw else None, ts, ts)).lastrowid


def row(conn, tx):
    return conn.execute("SELECT * FROM transmissions WHERE id = ?", (tx,)).fetchone()


def test_audio_raw_and_rows_by_age(conn, tmp_path):
    fresh = add(conn, tmp_path, 10)
    old_audio = add(conn, tmp_path, 100)             # > 90 days: audio + raw go, row stays
    ancient = add(conn, tmp_path, 400)               # > 365 days: whole row goes
    queued_old = add(conn, tmp_path, 400, status="queued")   # never delete in-flight rows
    f_old = tmp_path / row(conn, old_audio)["audio_path"]
    f_ancient = tmp_path / row(conn, ancient)["audio_path"]
    rep = retention.run(conn, cfg(tmp_path), now=NOW)
    assert rep.rows_deleted == 1 and rep.audio_clips == 1 and rep.raw_trimmed == 1
    assert row(conn, ancient) is None and not f_ancient.exists()
    r = row(conn, old_audio)
    assert r["audio_path"] is None and r["transcript"] == "clear" and r["raw_status"] is None
    assert not f_old.exists()
    assert row(conn, fresh)["audio_path"] and row(conn, fresh)["raw_status"]
    assert row(conn, queued_old) is not None
    assert rep.audio_bytes > 0


def test_search_entries_removed_with_rows(conn, tmp_path):
    tx = add(conn, tmp_path, 400, transcript="structure fire")
    assert conn.execute("SELECT COUNT(*) FROM transmissions_fts WHERE transmissions_fts MATCH 'structure'").fetchone()[0] == 1
    retention.run(conn, cfg(tmp_path), now=NOW)
    assert row(conn, tx) is None
    for t in ("transmissions_fts", "transmissions_tri"):
        assert conn.execute(f"SELECT COUNT(*) FROM {t} WHERE {t} MATCH '\"structure\"'").fetchone()[0] == 0


def test_zero_means_keep_forever(conn, tmp_path):
    tx = add(conn, tmp_path, 5000)
    rep = retention.run(conn, cfg(tmp_path, audio_days=0, raw_days=0, transcript_days=0),
                        now=NOW)
    assert not rep.anything and row(conn, tx)["audio_path"] and row(conn, tx)["raw_status"]


def test_dry_run_changes_nothing(conn, tmp_path):
    txs = [add(conn, tmp_path, 100 + i) for i in range(3)] + [add(conn, tmp_path, 500)]
    before = conn.execute("SELECT COUNT(*), COUNT(audio_path) FROM transmissions").fetchone()
    rep = retention.run(conn, cfg(tmp_path), dry_run=True, now=NOW)
    assert rep.audio_clips == 3 and rep.rows_deleted == 1 and rep.summary().startswith("would remove")
    assert conn.execute("SELECT COUNT(*), COUNT(audio_path) FROM transmissions").fetchone() == before
    assert all((tmp_path / row(conn, t)["audio_path"]).exists() for t in txs)


def test_batching_and_missing_files(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(retention, "BATCH", 2)
    txs = [add(conn, tmp_path, 100 + i) for i in range(5)]
    (tmp_path / row(conn, txs[0])["audio_path"]).unlink()          # already gone
    rep = retention.run(conn, cfg(tmp_path), now=NOW)
    assert rep.audio_clips == 5 and rep.missing_files == 1
    assert conn.execute("SELECT COUNT(audio_path) FROM transmissions").fetchone()[0] == 0


def test_empty_clip_folders_pruned_sessions_untouched(conn, tmp_path):
    add(conn, tmp_path, 100)
    sess = tmp_path / "sessions" / "20260930T000000Z" / "clips"
    sess.mkdir(parents=True)
    write_wav(sess / "0001.wav", np.ones(80, np.int16))
    retention.run(conn, cfg(tmp_path), now=NOW)
    assert not any((tmp_path / "clips").rglob("*"))                # empty dirs removed
    assert (sess / "0001.wav").exists()                            # labelled clips kept


def test_events_commands_prompt_tests(conn, tmp_path):
    statestore.event(conn, "capture", "info", "x", "old")
    statestore.event(conn, "capture", "info", "y", "new")
    conn.execute("UPDATE system_events SET at = ? WHERE kind = 'x'", ((NOW - timedelta(days=40)).isoformat(),))
    conn.execute("UPDATE system_events SET at = ? WHERE kind = 'y'", ((NOW - timedelta(days=1)).isoformat(),))
    c1 = commands.post(conn, "capture", "reconnect")
    conn.execute("UPDATE commands SET created_at = ?, done_at = ? WHERE id = ?",
                 ((NOW - timedelta(days=40)).isoformat(), (NOW - timedelta(days=40)).isoformat(), c1))
    for i in range(5):
        conn.execute("INSERT INTO prompt_evals (created_at, status) VALUES (?, ?)",
                     (f"2027-05-0{i + 1}", "running" if i == 0 else "done"))
    rep = retention.run(conn, cfg(tmp_path, prompt_tests_keep=2), now=NOW)
    assert rep.events_deleted == 1 and rep.commands_deleted == 1
    kinds = [e["kind"] for e in statestore.recent_events(conn)]
    assert "x" not in kinds and "y" in kinds and "retention" in kinds          # summary event
    # newest 2 kept, plus the running one (never deleted mid-run)
    assert sorted(r[0] for r in conn.execute("SELECT status FROM prompt_evals")) == ["done", "done", "running"]
    last = retention.last_run(conn)
    assert last and "removed" in last["summary"]


def test_vacuum_only_on_request(conn, tmp_path):
    """The hourly pass never compacts (it would hold the write lock for seconds);
    `retention --vacuum` does."""
    c = cfg(tmp_path)
    for i in range(200):
        add(conn, tmp_path, 400 + i, audio=False, transcript="x" * 500)
    for hour in range(24):
        at = NOW.astimezone().replace(hour=hour).astimezone(UTC)
        assert not retention.run(conn, c, now=at).vacuumed
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    rep = retention.run(conn, c, now=NOW, vacuum=True)
    assert rep.vacuumed and "database compacted" in rep.summary()
    assert conn.execute("PRAGMA page_count").fetchone()[0] < pages


def test_status_page_shows_retention(env):
    conn, client, d = env
    html = client.get("/status").text
    assert "Last cleanup" in html and "not run yet" in html and "audio 90 days" in html
