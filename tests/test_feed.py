"""Live feed, paging and jump-to (queries + pages) over a synthetic DB."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from scanner_transcriber.audio import write_wav
from scanner_transcriber.web import feed

import numpy as np

T0 = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)  # 12:00 EDT


def add(conn, data_dir, t, tgid="1101", dur=2.0, transcript="Medic 3 en route",
        status="done", no_speech=0, flags=None, audio=True, channel=None):
    started = T0 + timedelta(seconds=t)
    ended = started + timedelta(seconds=dur)
    now = datetime.now(UTC).isoformat()
    key = f"sys:Sys|tg:{tgid}"          # like production: every row's talkgroup is in channels
    conn.execute("""INSERT INTO channels (key, system, department, channel, tgid, first_heard_at,
                                          last_heard_at, color)
                    VALUES (?, 'Sys', 'County Sheriff', ?, ?, ?, ?,
                            (SELECT COUNT(*) FROM channels) % 10)
                    ON CONFLICT(key) DO UPDATE SET last_heard_at = MAX(last_heard_at,
                                                                       excluded.last_heard_at)""",
                 (key, channel or f"Ch{tgid}", tgid, started.isoformat(), started.isoformat()))
    cur = conn.execute(
        """INSERT INTO transmissions (channel_key, started_at, ended_at, duration_ms, system,
               department, channel, tgid, unit_id, status, transcript, no_speech, flags,
               audio_format, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'Sys', 'County Sheriff', ?, ?, 'UID:3219140', ?, ?, ?, ?, 'wav', ?, ?)""",
        (key, started.isoformat(timespec="milliseconds"), ended.isoformat(timespec="milliseconds"),
         int(dur * 1000), channel or f"Ch{tgid}", tgid, status, transcript, no_speech,
         json.dumps(flags or []), now, now))
    tx = cur.lastrowid
    if audio:
        rel = f"clips/{tx}.wav"
        (data_dir / "clips").mkdir(exist_ok=True)
        write_wav(data_dir / rel, np.ones(800, np.int16))
        conn.execute("UPDATE transmissions SET audio_path = ? WHERE id = ?", (rel, tx))
    return tx


def ids(rows):
    return [r["id"] for r in rows]


# ---- queries ---------------------------------------------------------------------------------

def test_keyset_paging_is_exact_while_rows_arrive(env):
    conn, _, d = env
    all_ids = [add(conn, d, t * 60) for t in range(25)]          # oldest → newest
    page1 = feed.latest(conn, limit=10)
    assert ids(page1) == all_ids[::-1][:10]
    add(conn, d, 99 * 60)                                        # new row arrives meanwhile
    cur = feed.encode_cursor(page1[-1]["started_at"], page1[-1]["id"])
    page2 = feed.older(conn, cur, limit=10)
    assert ids(page2) == all_ids[::-1][10:20]                     # no dupes, no gaps


def test_same_start_time_tiebreak_by_id(env):
    conn, _, d = env
    a, b = add(conn, d, 0), add(conn, d, 0)
    [top] = feed.latest(conn, limit=1)
    assert top["id"] == b
    assert ids(feed.older(conn, feed.encode_cursor(top["started_at"], b), limit=5)) == [a]


def test_newer_with_same_start_time_tiebreak_by_id(env):
    conn, _, d = env
    a, b, c = add(conn, d, 0), add(conn, d, 0), add(conn, d, 0)
    r = conn.execute("SELECT started_at FROM transmissions WHERE id=?", (a,)).fetchone()
    assert ids(feed.newer(conn, feed.encode_cursor(r[0], a), limit=5)) == [c, b]


def test_known_talkgroups_lists_every_talkgroup_busiest_first(env):
    conn, _, d = env
    add(conn, d, -400 * 86400, tgid="1003")                     # heard once, a year ago
    for i in range(3):
        add(conn, d, i, tgid="1201")
    add(conn, d, 5, tgid="1101")
    now = datetime.now(UTC)
    conn.execute("UPDATE transmissions SET started_at = ? WHERE tgid != '1003'",
                 (now.isoformat(timespec="milliseconds"),))
    tgs = feed.known_talkgroups(conn)
    assert [t["tgid"] for t in tgs] == ["1201", "1101", "1003"]
    assert tgs[0]["n"] == 3 and tgs[2]["n"] == 0 and tgs[0]["name"] == "Ch1201"


def test_newer_returns_newest_first(env):
    conn, _, d = env
    x = [add(conn, d, t * 60) for t in range(5)]
    r = conn.execute("SELECT started_at FROM transmissions WHERE id=?", (x[1],)).fetchone()
    assert ids(feed.newer(conn, feed.encode_cursor(r[0], x[1]), limit=10)) == x[:1:-1]


def test_around_anchor(env):
    conn, _, d = env
    x = [add(conn, d, t * 60) for t in range(10)]                # 12:00 … 12:09 EDT
    after, before = feed.around(conn, T0 + timedelta(minutes=5), each_side=3)
    assert ids(after) == [x[7], x[6], x[5]] and ids(before) == [x[4], x[3], x[2]]


def test_talkgroup_filter_and_hide_no_speech(env):
    conn, _, d = env
    a = add(conn, d, 0, tgid="1101")
    add(conn, d, 60, tgid="1201")
    c = add(conn, d, 120, tgid="1101", no_speech=1, transcript="")
    assert ids(feed.latest(conn, tgs=["1101"])) == [c, a]
    assert ids(feed.latest(conn, tgs=["1101"], hide_no_speech=True)) == [a]


def test_exchange_grouping_and_day_breaks(env):
    conn, _, d = env
    add(conn, d, 0, tgid="1101")            # 12:00:00-02
    add(conn, d, 5, tgid="1101")            # reply 3 s later → same exchange
    add(conn, d, 30, tgid="1101")           # 23 s gap → new exchange
    add(conn, d, 33, tgid="1201")           # different talkgroup
    rows = feed.present(feed.latest(conn), None)
    assert [r.cont for r in rows] == [False, False, False, True]  # newest first; label on top
    days = feed.mark_days(rows)
    assert days[0].day_break and all(r.day_break is None for r in days[1:])


# ---- pages -----------------------------------------------------------------------------------

def test_live_page_renders_rows_and_chips(env):
    conn, client, d = env
    add(conn, d, 0, transcript="Traffic stop Mill Road")
    add(conn, d, 60, status="queued", transcript=None)
    add(conn, d, 120, no_speech=1, transcript="", flags=["hallucination"])
    # chips list talkgroups heard in the last 24 h: make the rows recent
    conn.execute("UPDATE transmissions SET started_at = strftime('%Y-%m-%dT%H:%M:%fZ','now','-5 minutes')")
    html = client.get("/").text
    assert "Traffic stop Mill Road" in html and "transcribing…" in html
    assert 'class="tx' in html and "no-speech" in html and "hallucination" in html
    assert 'class="chip-tg"' in html and "Ch1101" in html
    assert 'type="datetime-local"' in html and "Hide no-speech" in html


def test_load_older_fragment_and_end(env):
    conn, client, d = env
    for t in range(15):
        add(conn, d, t * 60)
    html = client.get("/").text
    assert "Load older" in html
    cursor = html.split('/fragments/feed?before=')[1].split('&')[0]
    frag = client.get(f"/fragments/feed?before={cursor}").text
    assert frag.count('id="tx-') == 5 and "everything recorded" in frag


def test_jump_to_anchors_with_marker_and_back_to_live(env):
    conn, client, d = env
    for t in range(30):
        add(conn, d, t * 60, transcript=f"call {t}")      # 12:00 … 12:29 EDT
    html = client.get("/?at=2026-09-30T12:10").text
    assert 'id="anchor"' in html and "Back to live" in html and "Showing around" in html
    top, bottom = html.split('id="anchor"')
    assert "call 10" in top and "call 9" in bottom           # 12:10 is at/after the marker
    assert "call 14" in top and "call 5" in bottom and "call 29" not in html
    assert "Newer" in top and "Load older" in bottom


def test_jump_combines_with_talkgroup_filter(env):
    conn, client, d = env
    add(conn, d, 0, tgid="1101", transcript="sheriff call")
    add(conn, d, 60, tgid="1201", transcript="town call")
    html = client.get("/?at=2026-09-30T12:00&tg=1201").text
    assert "town call" in html and "sheriff call" not in html


def test_jump_to_bad_time_shows_message(env):
    _, client, _ = env
    html = client.get("/?at=not-a-time").text
    assert "Couldn&#39;t read the date/time" in html


@pytest.mark.parametrize("local,utc", [
    ("2026-11-01T01:30", "2026-11-01T05:30"),   # repeated hour (EDT→EST): first occurrence
    ("2026-03-08T02:30", "2026-03-08T07:30"),   # nonexistent hour (EST→EDT): shifted, no crash
    ("2026-07-04T21:00", "2026-07-05T01:00"),
])
def test_jump_parses_local_time_across_dst(env, local, utc):
    conn, client, d = env
    target = datetime.fromisoformat(utc + "+00:00")
    conn.execute(
        """INSERT INTO transmissions (started_at, ended_at, duration_ms, tgid, status,
               transcript, created_at, updated_at)
           VALUES (?, ?, 1000, '1101', 'done', 'at the target', ?, ?)""",
        (target.isoformat(timespec="milliseconds"),
         (target + timedelta(seconds=1)).isoformat(timespec="milliseconds"),
         target.isoformat(), target.isoformat()))
    html = client.get(f"/?at={local}").text
    top = html.split('id="anchor"')[0]
    assert "at the target" in top      # the target row is at/after the anchor, i.e. parsed ≤ target


def test_purged_audio_note_and_row(env):
    conn, client, d = env
    add(conn, d, 0, audio=False)
    html = client.get("/?at=2026-09-30T12:00").text
    assert "removed by retention" in html


# ---- API + audio -------------------------------------------------------------------------------

def test_api_transmissions(env):
    conn, client, d = env
    x = [add(conn, d, t * 60, flags=["repeat"]) for t in range(5)]
    j = client.get("/api/transmissions?limit=2").json()
    assert [i["id"] for i in j["items"]] == [x[4], x[3]]
    assert j["items"][0]["flags"] == ["repeat"] and "audio_path" not in j["items"][0]
    j2 = client.get(f"/api/transmissions?limit=10&before={j['items'][1]['cursor']}").json()
    assert [i["id"] for i in j2["items"]] == [x[2], x[1], x[0]]


def test_tg_chips_fragment_and_empty_placeholder(env):
    conn, client, d = env
    html = client.get("/").text
    assert 'id="tg-chips"' in html and 'id="tg-chips" aria-label="Filter by talkgroup" hidden' in html
    add(conn, d, 0, tgid="1201", channel="Dispatch")
    add(conn, d, 60, tgid="1201", channel="Dispatch")
    conn.execute("UPDATE transmissions SET started_at = ?", (datetime.now(UTC).isoformat(),))
    frag = client.get("/fragments/tg-chips?tg=1201").text
    assert 'id="tg-chips"' in frag and " hidden>" not in frag.split("\n")[0]
    assert "TG 1201" in frag and '<span class="n">2</span>' in frag
    assert 'aria-current="true"' in frag                       # the selected chip stays selected


def test_live_page_carries_since_and_pause(env):
    conn, client, d = env
    add(conn, d, 0)
    html = client.get("/").text
    assert 'data-since="20' in html and 'id="pause"' in html and 'id="new-pill"' in html
    assert "/static/app.js" in html
    anchored = client.get("/?at=2026-09-30T12:00").text
    assert 'id="pause"' not in anchored and 'data-mode="anchor"' in anchored


def test_live_page_carries_row_cap(env):
    conn, client, d = env
    add(conn, d, 0)
    assert 'data-max-rows="300"' in client.get("/").text
