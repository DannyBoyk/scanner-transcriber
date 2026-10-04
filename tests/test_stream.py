"""Listen live: the shared station (clip pickup, levelling, pacing, fan-out, listeners) and the
/stream.mp3 endpoint. Real DB + WAV files; timing scaled down."""
import asyncio
import time

import numpy as np

from scanner_transcriber.audio import write_wav
from scanner_transcriber.web.stream import STREAM_RATE, Station

from test_feed import add


def station(d, **kw):
    opts = dict(chunk_s=0.05, ahead_s=0.2, poll_s=0.05, pause_s=0.1, idle_stop_s=0.3,
                backlog_s=0.5, intro_s=0.1)
    opts.update(kw)
    return Station(d / "scanner.db", d, **opts)


def loud_clip(conn, d, t, seconds=0.5, **kw):
    """A row whose WAV is a real tone (the test_feed helper writes near-silence)."""
    tx = add(conn, d, t, **kw)
    rel = conn.execute("SELECT audio_path FROM transmissions WHERE id = ?", (tx,)).fetchone()[0]
    n = int(seconds * 8000)
    write_wav(d / rel, (8000 * np.sin(np.arange(n) * 0.3)).astype(np.int16))
    return tx


def mp3_frames(data: bytes) -> list[tuple[int, int, int]]:
    """(version bits, layer bits, sample rate Hz) for each MPEG audio frame header found."""
    rates = {0b10: (22050, 24000, 16000), 0b11: (44100, 48000, 32000), 0b00: (11025, 12000, 8000)}
    out, i = [], 0
    while i + 4 <= len(data):
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            ver, layer = (data[i + 1] >> 3) & 3, (data[i + 1] >> 1) & 3
            sr_idx = (data[i + 2] >> 2) & 3
            if ver in rates and sr_idx < 3 and layer == 0b01:
                out.append((ver, layer, rates[ver][sr_idx]))
                i += 4
                continue
        i += 1
    return out


async def collect(st, seconds, delay=0.0):
    """One listener: join after `delay`, read for `seconds`, then leave. Returns the bytes."""
    await asyncio.sleep(delay)
    got = bytearray()
    gen = st.listen()
    end = time.monotonic() + seconds
    try:
        async for chunk in gen:
            got += chunk
            if time.monotonic() >= end:
                break
    finally:
        await gen.aclose()
    return bytes(got)


def test_plays_only_new_clips_including_no_speech_and_hidden(env):
    conn, _, d = env
    loud_clip(conn, d, 0)                                       # before the station starts
    st = station(d)

    async def main():
        listener = asyncio.ensure_future(collect(st, 1.2))
        await asyncio.sleep(0.3)                                # station has picked its "now"
        a = loud_clip(conn, d, 60)
        b = loud_clip(conn, d, 120, no_speech=1)                # no-speech clips play too
        c = loud_clip(conn, d, 180, tgid="1518")
        conn.execute("UPDATE channels SET hidden = 1, ignored = 1 WHERE tgid = '1518'")
        await listener
        return a, b, c
    a, b, c = asyncio.run(main())
    assert st.clips_played == 3 and st.last_id == c


def test_missing_audio_file_is_skipped(env):
    conn, _, d = env
    st = station(d)

    async def main():
        listener = asyncio.ensure_future(collect(st, 0.8))
        await asyncio.sleep(0.3)
        tx = loud_clip(conn, d, 60)
        rel = conn.execute("SELECT audio_path FROM transmissions WHERE id=?", (tx,)).fetchone()[0]
        (d / rel).unlink()                                      # retention / clean start
        await listener
    asyncio.run(main())
    assert st.clips_played == 0


def test_stream_is_valid_16k_mpeg2_mp3_paced_in_real_time(env):
    conn, _, d = env
    st = station(d, ahead_s=0.3)
    data = asyncio.run(collect(st, 1.5))
    frames = mp3_frames(data)
    assert frames and all(f == (0b10, 0b01, STREAM_RATE) for f in frames)   # MPEG-2 Layer III 16 kHz
    seconds = len(frames) * 576 / STREAM_RATE                   # 576 samples per MPEG-2 L3 frame
    assert 1.2 <= seconds <= 1.5 + 0.3 + 0.3                    # ≈ real time (+ the head start)


def test_two_listeners_hear_the_same_bytes_and_a_late_joiner_gets_the_recent_audio(env):
    conn, _, d = env
    st = station(d, intro_s=0.2)

    async def main():
        a = asyncio.ensure_future(collect(st, 1.4))
        b = asyncio.ensure_future(collect(st, 0.6, delay=0.6))    # joins later
        await asyncio.sleep(0.2)
        loud_clip(conn, d, 60, seconds=1.0)
        return await a, await b
    a, b = asyncio.run(main())
    assert len(b) > 0 and b[:200] in a                          # same stream, from the ring buffer


def test_a_stalled_listener_is_dropped_without_holding_up_others(env):
    conn, _, d = env
    st = station(d, backlog_s=0.3)

    async def main():
        stalled = st.listen()
        await stalled.__anext__()                               # joins, then never reads again
        healthy = await collect(st, 1.5)
        return healthy, len(st.listeners)
    healthy, left = asyncio.run(main())
    assert len(healthy) > 3000 and left == 0                    # the healthy one kept going


def test_station_stops_when_idle_and_restarts_at_the_new_now(env):
    conn, _, d = env
    st = station(d)

    async def main():
        await collect(st, 0.3)
        assert st.state == "playing"
        await asyncio.sleep(0.6)                                # > idle_stop_s with nobody listening
        stopped = st.state
        loud_clip(conn, d, 60)                                  # arrives while stopped: not played
        await collect(st, 0.4)
        return stopped
    assert asyncio.run(main()) == "idle"
    assert st.clips_played == 0


def test_endpoint_page_header_and_status(env):
    conn, client, d = env
    st = client.app.state.station
    real_listen = st.listen

    async def first_chunks(key=None):   # the test client waits for a body to end; this one never would
        n = 0
        async for chunk in real_listen(key):
            yield chunk
            n += 1
            if n >= 12:
                return
    st.listen = first_chunks
    with client.stream("GET", "/stream.mp3", headers={"Range": "bytes=0-1"}) as r:   # Safari's probe
        assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg"
        assert "content-length" not in r.headers and "accept-ranges" not in r.headers
        got = b""
        for chunk in r.iter_bytes():
            got += chunk
            if len(got) > 2000:
                break
    assert mp3_frames(got)
    html = client.get("/listen").text
    assert 'id="listen-btn"' in html and 'id="listen-audio"' in html and "/stream.mp3" in html
    assert 'class="listen-link"' in client.get("/status").text           # header, every page
    assert "Live stream" in client.get("/status").text
    assert "listen" in client.get("/fragments/listen-info").text



def test_safaris_two_connections_count_as_one_listener(env):
    conn, _, d = env
    st = station(d)

    async def main():
        probe, real = st.listen("1790860826575"), st.listen("1790860826575")   # same page token
        other, vlc = st.listen("1790860865487"), st.listen()                   # a computer, VLC
        for g in (probe, real, other, vlc):
            await g.__anext__()
        count = st.info()["listeners"]
        for g in (probe, real, other, vlc):
            await g.aclose()
        return count
    assert asyncio.run(main()) == 3
