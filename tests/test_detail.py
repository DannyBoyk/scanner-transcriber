"""Transmission detail page, JSON detail, and audio serving (Range, download, purged)."""
import json

from test_feed import add


def enrich(conn, tx, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE transmissions SET {sets} WHERE id = ?", (*cols.values(), tx))


def test_detail_page_shows_everything(env):
    conn, client, d = env
    prev = add(conn, d, 0, transcript="Dispatch to 121")
    tx = add(conn, d, 5, transcript="121 traffic stop Mill Road")
    nxt = add(conn, d, 10, transcript="Copy 121")
    add(conn, d, 7, tgid="1201", transcript="other talkgroup")
    enrich(conn, tx, unit_ids=json.dumps(["UID:3219140", "UID:3213277"]), site="County Simulcast",
           frequency_hz=851512500, rssi=-60, raw_status="<ScannerInfo Mode=\"Trunk Scan\"/>",
           status_changes=json.dumps([{"sample": 0, "wall": "2026-09-30T16:00:05+00:00",
                                       "fields": {"tgid": "1101", "unit_id": "UID:3219140"}}]),
           raw_transcript="121, traffic stop, Mill Road", stt_model="whisper-small.en",
           stt_ms=4700, transcribed_at="2026-09-30T16:00:14+00:00")
    html = client.get(f"/t/{tx}").text
    assert "121 traffic stop Mill Road" in html                       # transcript
    assert "Wednesday, September 30 2026 at 12:00:05" in html and "16:00:05 UTC" in html
    assert "#3219140, #3213277" in html and "851.5125 MHz" in html and "-60 dBm" in html
    assert "original output" in html                          # raw differs
    assert "whisper-small.en" in html and "4.7 s" in html and "7 s after it ended" in html
    assert f'/api/transmissions/{tx}/audio' in html and "Download" in html
    assert "Dispatch to 121" in html and "Copy 121" in html and "other talkgroup" not in html
    assert f'href="/t/{prev}">Previous' in html and f'href="/t/{nxt}">Next' in html
    assert "Raw scanner data" in html and "unit_id=UID:3219140" in html
    assert "Back to the feed at 12:00" in html


def test_detail_flags_explained_and_alt(env):
    conn, client, d = env
    tx = add(conn, d, 0, transcript="Thank you.", flags=["hallucination"])
    enrich(conn, tx, alt_transcript="thank you medic three")
    html = client.get(f"/t/{tx}").text
    assert "checked with a second model" in html and "Second model heard" in html


def test_detail_pending_polls_until_done(env):
    conn, client, d = env
    tx = add(conn, d, 0, status="queued", transcript=None)
    html = client.get(f"/t/{tx}").text
    assert "Transcribing…" in html and f'hx-get="/fragments/tx-text/{tx}"' in html
    r = client.get(f"/fragments/tx-text/{tx}")
    assert r.status_code == 200                                          # keep polling
    enrich(conn, tx, status="done", transcript="Go ahead")
    r = client.get(f"/fragments/tx-text/{tx}")
    assert r.status_code == 286 and "Go ahead" in r.text and "hx-get" not in r.text  # stop


def test_detail_not_found_and_purged(env):
    conn, client, d = env
    r = client.get("/t/99999")
    assert r.status_code == 404 and "exist" in r.text
    tx = add(conn, d, 0, audio=False)
    assert "removed by retention" in client.get(f"/t/{tx}").text


def test_api_detail(env):
    conn, client, d = env
    tx = add(conn, d, 0, flags=["repeat"])
    j = client.get(f"/api/transmissions/{tx}").json()
    assert j["id"] == tx and j["flags"] == ["repeat"] and j["has_audio"]
    assert j["audio_url"] == f"/api/transmissions/{tx}/audio" and "audio_path" not in j
    assert client.get("/api/transmissions/99999").status_code == 404


def test_audio_download_name(env):
    conn, client, d = env
    tx = add(conn, d, 5)
    r = client.get(f"/api/transmissions/{tx}/audio?download=1")
    assert r.headers["content-disposition"] == \
        f'attachment; filename="2026-09-30_120005_TG1101_{tx}.wav"'
    assert "content-disposition" not in client.get(f"/api/transmissions/{tx}/audio").headers


def test_audio_ranges_for_safari(env):
    conn, client, d = env
    tx = add(conn, d, 0)                     # 800 samples → 44 + 1600 = 1644 bytes
    url = f"/api/transmissions/{tx}/audio"
    full = client.get(url)
    assert full.status_code == 200 and len(full.content) == 1644
    assert full.headers["content-type"] == "audio/wav"
    assert full.headers.get("accept-ranges") == "bytes"
    r = client.get(url, headers={"Range": "bytes=0-1"})        # Safari's first probe
    assert r.status_code == 206 and r.content == full.content[:2]
    assert r.headers["content-range"] == "bytes 0-1/1644"
    r = client.get(url, headers={"Range": "bytes=1000-"})
    assert r.status_code == 206 and r.content == full.content[1000:]
    r = client.get(url, headers={"Range": "bytes=-100"})
    assert r.status_code == 206 and r.content == full.content[-100:]
    r = client.get(url, headers={"Range": "bytes=5000-6000"})
    assert r.status_code == 416


def test_audio_is_levelled_with_range_support_and_download_is_original(env):
    import io
    import wave

    import numpy as np
    from test_feed import add
    conn, client, d = env
    tx = add(conn, d, 0)
    path = d / conn.execute("SELECT audio_path FROM transmissions WHERE id=?", (tx,)).fetchone()[0]
    original = path.read_bytes()
    r = client.get(f"/api/transmissions/{tx}/audio")
    assert r.status_code == 200 and r.headers["accept-ranges"] == "bytes"
    assert r.content != original                                          # levelled copy
    with wave.open(io.BytesIO(r.content)) as w:
        assert w.getframerate() == 8000 and w.getnframes() == 800
        pcm = np.frombuffer(w.readframes(800), "<i2")
    assert np.abs(pcm).max() > 1                                          # boosted from 1
    part = client.get(f"/api/transmissions/{tx}/audio", headers={"Range": "bytes=0-1"})
    assert part.status_code == 206 and part.content == r.content[:2]
    assert part.headers["content-range"] == f"bytes 0-1/{len(r.content)}"
    tail = client.get(f"/api/transmissions/{tx}/audio", headers={"Range": "bytes=-10"})
    assert tail.status_code == 206 and tail.content == r.content[-10:]
    bad = client.get(f"/api/transmissions/{tx}/audio", headers={"Range": f"bytes={len(r.content)}-"})
    assert bad.status_code == 416
    dl = client.get(f"/api/transmissions/{tx}/audio?download=1")
    assert dl.content == original and "attachment" in dl.headers["content-disposition"]


def test_audio_missing_and_purged(env):
    conn, client, d = env
    tx = add(conn, d, 0, audio=False)
    assert client.get(f"/api/transmissions/{tx}/audio").status_code == 410
    assert client.get("/api/transmissions/99999/audio").status_code == 404
