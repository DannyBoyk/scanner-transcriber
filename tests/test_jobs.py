import multiprocessing as mp
from datetime import UTC, datetime, timedelta

from test_db import clip

from scanner_transcriber import db, jobs
from scanner_transcriber.clipstore import ClipStore


def add(conn, tmp_path, t=0.0, **kw):
    return ClipStore(conn, tmp_path, "sds200-1").save(clip(t, **kw))


def test_claim_oldest_first_then_priority(conn, tmp_path):
    a = add(conn, tmp_path, 10)
    b = add(conn, tmp_path, 0)
    c = add(conn, tmp_path, 20, tgid="9999")
    conn.execute("UPDATE transmissions SET priority = 5 WHERE id = ?", (c,))
    assert [jobs.claim(conn, "w")["id"] for _ in range(3)] == [c, b, a]
    assert jobs.claim(conn, "w") is None


def test_claim_marks_processing(conn, tmp_path):
    tx = add(conn, tmp_path)
    row = jobs.claim(conn, "w1")
    assert row["id"] == tx and row["attempts"] == 1 and row["audio_path"]
    r = conn.execute("SELECT status, claimed_by FROM transmissions WHERE id=?", (tx,)).fetchone()
    assert tuple(r) == ("processing", "w1")


def _race(path, q):
    c = db.connect(path)
    got = []
    while (row := jobs.claim(c, mp.current_process().name)) is not None:
        got.append(row["id"])
    q.put(got)


def test_two_processes_never_claim_the_same_row(conn, tmp_path):
    ids = {add(conn, tmp_path, t) for t in range(40)}
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_race, args=(tmp_path / "scanner.db", q)) for _ in range(2)]
    for p in procs:
        p.start()
    results = [q.get(timeout=30) for _ in procs]
    for p in procs:
        p.join()
    claimed = results[0] + results[1]
    assert len(claimed) == len(set(claimed)) == 40 and set(claimed) == ids


def test_fail_requeues_then_fails_after_max_attempts(conn, tmp_path):
    tx = add(conn, tmp_path)
    for attempt in (1, 2):
        jobs.claim(conn, "w")
        assert jobs.fail(conn, tx, f"boom {attempt}", max_attempts=3) == "queued"
    jobs.claim(conn, "w")
    assert jobs.fail(conn, tx, "boom 3", max_attempts=3) == "failed"
    r = conn.execute("SELECT status, error FROM transmissions WHERE id=?", (tx,)).fetchone()
    assert tuple(r) == ("failed", "boom 3")


def test_recover_stale_claims(conn, tmp_path):
    old, fresh = add(conn, tmp_path, 0), add(conn, tmp_path, 1)
    jobs.claim(conn, "dead")
    jobs.claim(conn, "alive")
    long_ago = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    conn.execute("UPDATE transmissions SET claimed_at = ? WHERE id = ?", (long_ago, old))
    assert jobs.recover_stale(conn, timeout_s=120) == 1
    status = dict(conn.execute("SELECT id, status FROM transmissions").fetchall())
    assert status == {old: "queued", fresh: "processing"}


def test_release_mine_does_not_burn_an_attempt(conn, tmp_path):
    tx = add(conn, tmp_path)
    jobs.claim(conn, "w1")
    assert jobs.release_mine(conn, "w1") == 1
    r = conn.execute("SELECT status, attempts FROM transmissions WHERE id=?", (tx,)).fetchone()
    assert tuple(r) == ("queued", 0)


def test_finish_and_stats(conn, tmp_path):
    tx = add(conn, tmp_path)
    jobs.claim(conn, "w")
    jobs.finish(conn, tx, transcript="Medic 3 en route", raw_transcript="medic 3 en route",
                alt_transcript=None, flags=["repeat"], no_speech=False,
                stt_backend="ovwhisper", stt_model="small.en", stt_ms=1700)
    # clip() is dated 2026-09-30 12:00; move it to "now" so it's inside the stats window
    now = datetime.now(UTC)
    conn.execute("UPDATE transmissions SET ended_at = ? WHERE id = ?",
                 ((now - timedelta(seconds=8)).isoformat(), tx))
    s = jobs.stats(conn)
    assert s["by_status"] == {"done": 1} and s["done_last_window"] == 1
    assert 7 < s["latency_s_p50"] < 30
    assert s["rtf_mean"] == 1.7 and s["flagged_share"] == 1.0
    assert s["backends"] == {"ovwhisper": 1}


def test_recover_dead_worker_claims(conn, tmp_path):
    import os
    a, b = add(conn, tmp_path, 0), add(conn, tmp_path, 1)
    jobs.claim(conn, "nuc:999999999")          # no such pid
    jobs.claim(conn, f"nuc:{os.getpid()}")     # this process: alive
    assert jobs.recover_dead(conn, "nuc") == 1
    assert dict(conn.execute("SELECT id, status FROM transmissions").fetchall()) == \
        {a: "queued", b: "processing"}
