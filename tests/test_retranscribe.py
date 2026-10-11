"""Re-transcribe batches: selection, cap, low priority behind live clips, compare view."""

from test_feed import T0, add

from scanner_transcriber import jobs, retranscribe

HX = {"HX-Request": "true"}
SINCE = T0.isoformat()


def test_preview_and_start_keep_old_text(conn, tmp_path):
    c, d = conn, tmp_path
    a = add(c, d, 0, transcript="Director, can you turn")
    add(c, d, 60, tgid="1201", transcript="other tg")
    add(c, d, 120, no_speech=1, transcript="")
    add(c, d, 180, audio=False, transcript="purged audio")
    p = retranscribe.preview(c, SINCE, None, ["1101"], False)
    assert p["count"] == 1 and p["seconds"] > 0
    bid, n = retranscribe.start(c, SINCE, None, ["1101"], False)
    assert n == 1
    r = c.execute("SELECT status, priority, prev_transcript, batch_id FROM transmissions WHERE id=?",
                  (a,)).fetchone()
    assert tuple(r) == ("queued", retranscribe.PRIORITY, "Director, can you turn", bid)
    assert retranscribe.preview(c, SINCE, None, [], True)["count"] == 2   # other tg + no-speech


def test_batch_waits_behind_live_clips(conn, tmp_path):
    c, d = conn, tmp_path
    old = add(c, d, 0, transcript="old")
    retranscribe.start(c, SINCE, None, [], False)
    live = add(c, d, 600, status="queued", transcript=None)              # arrives later
    assert jobs.claim(c, "w")["id"] == live                              # live first
    assert jobs.claim(c, "w")["id"] == old


def test_cap(conn, tmp_path, monkeypatch):
    c, d = conn, tmp_path
    monkeypatch.setattr(retranscribe, "MAX_BATCH", 3)
    for i in range(5):
        add(c, d, i * 60, transcript=f"t{i}")
    p = retranscribe.preview(c, SINCE, None, [], False)
    assert p["count"] == 5 and p["capped"] == 3 and p["over_cap"]
    bid, n = retranscribe.start(c, SINCE, None, [], False)
    assert n == 3                                                        # oldest three
    assert [r[0] for r in c.execute("SELECT transcript FROM transmissions WHERE batch_id=? "
                                    "ORDER BY started_at", (bid,))] == ["t0", "t1", "t2"]


def test_progress_and_compare(conn, tmp_path):
    c, d = conn, tmp_path
    a = add(c, d, 0, transcript="Director, can you turn")
    b = add(c, d, 60, transcript="Go ahead")
    bid, _ = retranscribe.start(c, SINCE, None, [], False)
    [batch] = retranscribe.batches(c)
    assert batch["done"] == 0 and not batch["finished"]
    c.execute("UPDATE transmissions SET status='done', transcript='Direct, can you turn' WHERE id=?", (a,))
    c.execute("UPDATE transmissions SET status='done', transcript='Go ahead' WHERE id=?", (b,))
    [batch] = retranscribe.batches(c)
    assert batch["finished"] and batch["changed"] == 1
    assert [r["id"] for r in retranscribe.batch_rows(c, bid, changed_only=True)] == [a]


def test_admin_retranscribe_flow(admin_env):
    conn, client, d = admin_env
    a = add(conn, d, 0, transcript="Director")
    r = client.post("/admin/retranscribe/preview", data={"from": "2026-09-30T11:59"}, headers=HX)
    assert "<b>1</b> clip" in r.text and "Re-transcribe 1 clip" in r.text
    r = client.post("/admin/retranscribe", data={"from": "2026-09-30T11:59"}, headers=HX)
    assert "queued behind live traffic" in r.text and "Batch" in r.text
    assert conn.execute("SELECT status FROM transmissions WHERE id=?", (a,)).fetchone()[0] == "queued"
    conn.execute("UPDATE transmissions SET status='done', transcript='Direct' WHERE id=?", (a,))
    bid = conn.execute("SELECT batch_id FROM transmissions WHERE id=?", (a,)).fetchone()[0]
    html = client.get(f"/admin/batches/{bid}").text
    assert "<del>Director</del>" in html and "<ins>Direct</ins>" in html
    assert client.post("/admin/retranscribe", data={}, headers=HX).status_code == 400   # no range
    client.post("/logout")
    assert client.get(f"/admin/batches/{bid}", follow_redirects=False).status_code == 303
