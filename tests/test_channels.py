"""Talkgroup settings page, edits and their effects (feed, SSE, search, transcription)."""
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import numpy as np
from test_changes import parse_sse
from test_feed import T0

from scanner_transcriber.clipstore import ClipStore
from scanner_transcriber.models import Clip, ScannerStatus

HX = {"HX-Request": "true"}


def save_clip(conn, d, tgid, t=0.0, channel="Dispatch", dept="County Sheriff", transcript=None):
    st = ScannerStatus.from_summary({"system": "Sys", "department": dept, "channel": channel,
                                     "tgid": tgid, "mute": "Unmute"}, mono=0, wall=T0, sample=0)
    tx = ClipStore(conn, d, "sds200-1").save(Clip(
        start_sample=0, end_sample=8000, pcm=np.ones(8000, np.int16),
        started_at=T0 + timedelta(seconds=t), status=st, unit_ids=["UID:1"]))
    if transcript:
        conn.execute("UPDATE transmissions SET status='done', transcript=? WHERE id=?", (transcript, tx))
    return tx


def key(tgid):
    return f"sys:Sys|tg:{tgid}"


def url(tgid, suffix=""):
    return "/api/channels/" + quote(key(tgid), safe="/") + suffix


def test_channels_page_lists_talkgroups_with_activity(admin_env):
    conn, client, d = admin_env
    save_clip(conn, d, "1101", 0, transcript="a")
    save_clip(conn, d, "1101", 60, transcript="b")
    save_clip(conn, d, "1201", 30, channel="Dispatch", dept="Town Police", transcript="c")
    html = client.get("/channels").text
    assert html.count('class="ch') >= 2 and "TG 1101" in html and "Town Police" in html
    assert "Transcribe: on" in html and "Live feed: shown" in html
    assert 'href="/channels"' in client.get("/").text                    # nav link


def test_edit_guard_blocks_cross_site_posts(admin_env):
    conn, client, d = admin_env
    save_clip(conn, d, "1101")
    r = client.patch(url("1101"), data={"alias": "Hacked"})              # plain form, no header
    assert r.status_code == 403
    r = client.post(url("1101", "/transcribe-skipped"))
    assert r.status_code == 403
    assert conn.execute("SELECT alias FROM channels").fetchone()[0] is None


def test_alias_and_colour_show_everywhere(admin_env):
    conn, client, d = admin_env
    save_clip(conn, d, "1101", transcript="clear")
    r = client.patch(url("1101"), data={"alias": "Sheriff Dispatch"}, headers=HX)
    assert r.status_code == 200 and "Saved" in r.text and "Sheriff Dispatch" in r.text
    r = client.patch(url("1101"), json={"color": 7})
    assert r.json()["color"] == 7 and r.json()["name"] == "Sheriff Dispatch"
    live = client.get("/?at=2026-09-30T12:00").text
    assert "Sheriff Dispatch" in live and "--h: 248" in live            # palette[7] = 248
    assert "Sheriff Dispatch" in client.get("/search?q=clear").text
    client.patch(url("1101"), data={"alias": "  "}, headers=HX)          # empty → scanner name
    assert conn.execute("SELECT alias FROM channels").fetchone()[0] is None


def test_record_only_skips_new_clips_and_requeue(admin_env):
    conn, client, d = admin_env
    save_clip(conn, d, "1101", 0)
    client.patch(url("1101"), data={"ignored": "1"}, headers=HX)
    skipped = [save_clip(conn, d, "1101", t) for t in (10, 20)]
    assert {conn.execute("SELECT status FROM transmissions WHERE id=?", (s,)).fetchone()[0]
            for s in skipped} == {"skipped"}
    html = client.patch(url("1101"), data={"ignored": "0"}, headers=HX).text
    assert "Transcribe the 2 clips recorded while off" in html
    r = client.post(url("1101", "/transcribe-skipped"), headers=HX)
    assert "Queued 2 clips" in r.text
    assert [s for (s,) in conn.execute("SELECT status FROM transmissions ORDER BY id")] == \
        ["queued", "queued", "queued"]


def test_hidden_talkgroup_leaves_live_feed_but_not_search(admin_env):
    conn, client, d = admin_env
    save_clip(conn, d, "1101", 0, transcript="sheriff traffic")
    save_clip(conn, d, "1103", 30, channel="Tac 3", transcript="tac chatter")
    client.patch(url("1103"), data={"hidden": "1"}, headers=HX)
    live = client.get("/?at=2026-09-30T12:00").text
    assert "sheriff traffic" in live and "tac chatter" not in live
    assert "tac chatter" in client.get("/?at=2026-09-30T12:00&tg=1103").text   # picked → shown
    assert "<mark>chatter</mark>" in client.get("/search?q=chatter").text     # still searchable
    # live updates skip it too
    since = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    conn.execute("UPDATE transmissions SET updated_at = ?", (datetime.now(UTC).isoformat(),))
    client.app.state.sse_max_seconds = 0
    ids = [json.loads(e["data"])["id"] for e in parse_sse(
        client.get("/events", params={"feed": 1, "since": since}).text) if e["event"] == "tx"]
    tac = conn.execute("SELECT id FROM transmissions WHERE tgid='1103'").fetchone()[0]
    assert ids and tac not in ids


def test_bad_edits(admin_env):
    conn, client, d = admin_env
    save_clip(conn, d, "1101")
    assert client.patch(url("1101"), json={}).status_code == 400
    assert client.patch(url("1101"), json={"color": "red"}).status_code == 400
    assert client.patch(url("9999"), json={"alias": "x"}).status_code == 404


def test_api_channels(admin_env):
    conn, client, d = admin_env
    save_clip(conn, d, "1101")
    [c] = client.get("/api/channels").json()["items"]
    assert c["key"] == key("1101") and c["n_total"] == 1 and c["hidden"] == 0
