"""SSE change feed: no gaps, no duplicates, resume; and the /events stream itself."""
import json
from datetime import UTC, datetime, timedelta

from test_feed import add

from scanner_transcriber.web.changes import ChangeFeed, cursor_from_event_id, event_id


def at(offset_s):
    return (datetime.now(UTC) + timedelta(seconds=offset_s)).isoformat(timespec="milliseconds")


def touch(conn, tx, ts):
    conn.execute("UPDATE transmissions SET updated_at = ? WHERE id = ?", (ts, tx))


def test_created_then_updated_each_delivered_once(conn, tmp_path):
    cf = ChangeFeed(at(-60))
    tx = add(conn, tmp_path, 0, status="queued", transcript=None)
    touch(conn, tx, at(-10))
    assert [r["id"] for r in cf.poll(conn)] == [tx]
    assert cf.poll(conn) == []                                  # nothing new: no duplicate
    conn.execute("UPDATE transmissions SET status='done', transcript='clear', updated_at=? "
                 "WHERE id=?", (at(-5), tx))
    [r] = cf.poll(conn)
    assert r["transcript"] == "clear"                            # the update arrives


def test_late_commit_with_older_timestamp_is_not_missed(conn, tmp_path):
    cf = ChangeFeed(at(-60))
    a = add(conn, tmp_path, 0)
    touch(conn, a, at(-1))
    assert [r["id"] for r in cf.poll(conn)] == [a]
    # another process commits a row stamped 1.5 s *before* the newest we've seen
    b = add(conn, tmp_path, 10)
    touch(conn, b, at(-2.5))
    assert [r["id"] for r in cf.poll(conn)] == [b]


def test_bulk_update_with_one_timestamp_does_not_stall_the_feed(conn, tmp_path):
    """A re-transcribe batch stamps up to 2000 rows with the same updated_at."""
    ids = [add(conn, tmp_path, i, status="done") for i in range(1200)]
    cf = ChangeFeed(at(-60))
    conn.execute("UPDATE transmissions SET updated_at = ?", (at(-30),))
    got = []
    for _ in range(3):
        got += [r["id"] for r in cf.poll(conn)]
    assert sorted(got) == ids                                   # all of them, once each
    new = add(conn, tmp_path, 9999, status="queued")
    touch(conn, new, at(-1))
    assert [r["id"] for r in cf.poll(conn)] == [new]           # and live rows keep coming
    assert cf.poll(conn) == []


def test_many_late_commits_arrive_over_a_few_polls(conn, tmp_path):
    cf = ChangeFeed(at(-60))
    a = add(conn, tmp_path, 0)
    touch(conn, a, at(-1))
    assert [r["id"] for r in cf.poll(conn)] == [a]
    late = [add(conn, tmp_path, 10 + i) for i in range(700)]    # committed just behind it
    conn.execute(f"UPDATE transmissions SET updated_at = ? WHERE id IN "
                 f"({','.join('?' * len(late))})", (at(-2), *late))
    got = [r["id"] for r in cf.poll(conn)]
    assert len(got) == 500                                      # capped per poll
    got += [r["id"] for r in cf.poll(conn)]
    assert sorted(got) == late and cf.poll(conn) == []


def test_resume_from_event_id_replays_only_the_window(conn, tmp_path):
    rows = []
    for i, off in enumerate((-30, -20, -10)):
        tx = add(conn, tmp_path, i * 60)
        touch(conn, tx, at(off))
        rows.append(tx)
    first = ChangeFeed(at(-60)).poll(conn)
    eid = event_id(first[1])                                     # client saw up to the 2nd row
    resumed = ChangeFeed(cursor_from_event_id(eid)).poll(conn)
    assert [r["id"] for r in resumed][-1] == rows[2]             # the missed row
    assert rows[0] not in [r["id"] for r in resumed]             # outside the overlap window


def test_talkgroup_filter(conn, tmp_path):
    a = add(conn, tmp_path, 0, tgid="1101")
    b = add(conn, tmp_path, 60, tgid="1201")
    for tx in (a, b):
        touch(conn, tx, at(-5))
    assert [r["id"] for r in ChangeFeed(at(-60), ["1201"]).poll(conn)] == [b]


def parse_sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        ev = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            k, _, v = line.partition(": ")
            ev[k] = v
        if "event" in ev:
            events.append(ev)
    return events


def test_events_stream_sends_rows_and_status(env):
    conn, client, d = env
    since = at(-60)
    tx = add(conn, d, 0, transcript="Medic 3 en route")
    touch(conn, tx, at(-5))
    client.app.state.sse_max_seconds = 0
    r = client.get(f"/events?feed=1&since={since}")
    assert r.headers["content-type"].startswith("text/event-stream")
    evs = parse_sse(r.text)
    kinds = [e["event"] for e in evs]
    assert "status" in kinds and "tx" in kinds
    txe = next(e for e in evs if e["event"] == "tx")
    payload = json.loads(txe["data"])
    assert payload["id"] == tx and f'id="tx-{tx}"' in payload["item"]
    assert "Medic 3 en route" in payload["item"] and 'class="xhead"' in payload["head"]
    assert txe["id"].endswith(f"|{tx}")
    status = json.loads(next(e for e in evs if e["event"] == "status")["data"])
    assert status["level"] == "error" and "status-dot" in status["html"]


def test_events_without_feed_sends_only_status(env):
    conn, client, d = env
    tx = add(conn, d, 0)
    touch(conn, tx, at(-5))
    client.app.state.sse_max_seconds = 0
    kinds = [e["event"] for e in parse_sse(client.get(f"/events?since={at(-60)}").text)]
    assert kinds == ["status"]


def test_events_resume_via_last_event_id_header(env):
    conn, client, d = env
    old, new = add(conn, d, 0), add(conn, d, 60)
    touch(conn, old, at(-30))
    touch(conn, new, at(-5))
    client.app.state.sse_max_seconds = 0
    hdr = {"Last-Event-ID": f"{at(-10)}|{old}"}
    ids = [json.loads(e["data"])["id"] for e in parse_sse(client.get("/events?feed=1", headers=hdr).text)
           if e["event"] == "tx"]
    assert ids == [new]
