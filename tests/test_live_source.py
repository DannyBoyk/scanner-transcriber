"""SDS200NetworkSource reconnect/teardown logic with a fake session (no scanner)."""
import asyncio
import time

import numpy as np
import pytest

from scanner_transcriber.config import ScannerConfig
from scanner_transcriber.models import AudioFrame
from scanner_transcriber.source import sds200_network as net

from segsim import T0


class FakeSession:
    instances: list["FakeSession"] = []
    behaviours: list[str] = []  # per connection: "ok" (frames forever), "stall", "fail"

    def __init__(self, cfg, loop, queue, sample_offset):
        self.loop, self.queue, self.offset = loop, queue, sample_offset
        self.behaviour = FakeSession.behaviours.pop(0) if FakeSession.behaviours else "ok"
        self.last_audio_mono = self.last_status_mono = None
        self.phase = "control"
        self.stopped = 0
        self.sent = 0
        FakeSession.instances.append(self)

    @property
    def end_sample(self):
        return self.offset + self.sent * 320

    def start(self):
        if self.behaviour == "fail":                 # scanner unreachable (link down)
            raise OSError("connection refused")
        if self.behaviour == "boom":
            raise ValueError("unexpected library error")
        self.phase = "audio"                         # control channel answered
        if self.behaviour == "refused":              # audio lockout: 554 refused
            try:
                raise ConnectionRefusedError(111, "Connection refused")
            except ConnectionRefusedError as e:
                from sds200.exceptions import ScannerConnectionError
                raise ScannerConnectionError("Could not start SDS200 network audio") from e
        self.phase = "streaming"
        if self.behaviour == "ok":
            self.loop.call_soon_threadsafe(self._pump)

    def _pump(self):
        if self.stopped:
            return
        self.last_audio_mono = self.last_status_mono = time.monotonic()
        self.queue.put_nowait(AudioFrame(self.offset + self.sent * 320,
                                         np.zeros(320, np.int16), T0))
        self.sent += 1
        self.loop.call_later(0.01, self._pump)

    def stop(self):
        self.stopped += 1


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(net, "PARK_POLL_S", 0.05)      # parking lot checks 20x faster in tests
    FakeSession.instances, FakeSession.behaviours = [], []
    monkeypatch.setattr(net, "_LiveSession", FakeSession)


async def take(source, n, timeout=10):
    frames = []
    gen = source.events()
    try:
        async with asyncio.timeout(timeout):
            async for ev in gen:
                frames.append(ev)
                if len(frames) >= n:
                    break
    finally:
        await gen.aclose()
    return frames


def test_close_stops_session_exactly_once(tmp_path):
    src = net.SDS200NetworkSource(ScannerConfig(), tmp_path / "lock")
    frames = asyncio.run(take(src, 5))
    assert len(frames) == 5
    [s] = FakeSession.instances
    assert s.stopped == 1  # TEARDOWN path ran on generator close


def test_stall_triggers_reconnect_and_timeline_continues(tmp_path):
    FakeSession.behaviours = ["stall", "ok"]
    src = net.SDS200NetworkSource(ScannerConfig(), tmp_path / "lock", stale_s=0.3,
                                  backoff_max_s=0.1)
    frames = asyncio.run(take(src, 3))
    first, second = FakeSession.instances
    assert first.stopped == 1 and second.stopped == 1
    assert src.reconnects == 1
    assert frames[0].sample == first.end_sample  # new session continues the timeline


def test_connect_failure_backs_off_and_retries(tmp_path):
    FakeSession.behaviours = ["fail", "fail", "ok"]
    src = net.SDS200NetworkSource(ScannerConfig(), tmp_path / "lock", backoff_max_s=0.05)
    asyncio.run(take(src, 1))
    assert len(FakeSession.instances) == 3
    assert all(s.stopped == 1 for s in FakeSession.instances)


def test_second_owner_waits_and_reports_session_busy(tmp_path):
    lock = tmp_path / "lock"
    states = []

    async def both():
        a = net.SDS200NetworkSource(ScannerConfig(), lock)
        b = net.SDS200NetworkSource(ScannerConfig(), lock, busy_retry_s=0.05,
                                    on_state=lambda s, **i: states.append((s, i)))
        gen_a = a.events()
        await gen_a.__anext__()
        gen_b = b.events()
        task = asyncio.create_task(gen_b.__anext__())
        await asyncio.sleep(0.2)
        assert not task.done()                 # b is waiting, not failing
        assert states and states[0][0] == "session_busy"
        await gen_a.aclose()                   # a releases the lock → b connects
        ev = await asyncio.wait_for(task, 5)
        assert ev is not None
        assert ("connecting", {}) in states and ("streaming", {}) in states
        await gen_b.aclose()

    asyncio.run(both())


def test_state_reports_through_reconnect(tmp_path):
    FakeSession.behaviours = ["stall", "ok"]
    states = []
    src = net.SDS200NetworkSource(ScannerConfig(), tmp_path / "lock", stale_s=0.3,
                                  backoff_max_s=0.1, on_state=lambda s, **i: states.append((s, i)))
    asyncio.run(take(src, 3))
    names = [s for s, _ in states]
    assert names == ["connecting", "streaming", "reconnecting", "connecting", "streaming"]
    info = states[2][1]
    assert "no audio" in info["error"] and info["next_retry_at"]


def test_rtsp_refusal_reports_session_busy(tmp_path, monkeypatch):
    from sds200.rtsp import RtspStatusError
    from sds200.exceptions import ScannerConnectionError

    class Refused(FakeSession):
        def start(self):
            if len(FakeSession.instances) == 1:
                try:
                    raise RtspStatusError("SETUP", 453, "Not Enough Bandwidth")
                except RtspStatusError as e:
                    raise ScannerConnectionError("Could not start audio") from e
            super().start()

    monkeypatch.setattr(net, "_LiveSession", Refused)
    states = []
    src = net.SDS200NetworkSource(ScannerConfig(), tmp_path / "lock", busy_retry_s=0.05,
                                  on_state=lambda s, **i: states.append((s, i)))
    asyncio.run(take(src, 1))
    busy = [i for s, i in states if s == "session_busy"]
    assert busy and "453" in busy[0]["error"]


def test_unexpected_error_retries_instead_of_ending_capture(tmp_path):
    FakeSession.behaviours = ["boom", "ok"]
    src = net.SDS200NetworkSource(ScannerConfig(), tmp_path / "lock", backoff_max_s=0.05)
    frames = asyncio.run(take(src, 2))
    assert len(frames) == 2
    assert [s.stopped for s in FakeSession.instances] == [1, 1]   # TEARDOWN on both


# ---- audio lockout recovery (MSM,0) -----------------------------------------------------------

from dataclasses import replace as _replace


def recovery_source(tmp_path, monkeypatch, **cfg):
    reboots = []
    monkeypatch.setattr(net, "reboot_scanner", lambda host, port: reboots.append(time.monotonic()) or True)
    monkeypatch.setattr(net.SDS200NetworkSource, "REBOOT_SETTLE_S", 0.05)
    c = _replace(ScannerConfig(), auto_reboot_after_s=0.3, auto_reboot_grace_s=0,
                 auto_reboot_min_interval_s=0, **cfg)
    states = []
    src = net.SDS200NetworkSource(c, tmp_path / "lock", backoff_max_s=0.05,
                                  on_state=lambda s, **i: states.append((s, i)))
    return src, reboots, states


def run_for(src, seconds, n=10**9, until=None):
    """Consume the source for up to `seconds`, `n` events, or until `until()` is true
    (checked even while the source is retrying and yields nothing)."""
    async def main():
        gen = src.events()
        got = []

        async def consume():
            async for ev in gen:
                got.append(ev)
                if len(got) >= n:
                    return

        task = asyncio.ensure_future(consume())
        end = time.monotonic() + seconds
        while not task.done() and time.monotonic() < end and not (until and until()):
            await asyncio.sleep(0.02)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await gen.aclose()
        return got
    return asyncio.run(main())


def test_audio_lockout_reboots_the_scanner_then_audio_resumes(tmp_path, monkeypatch):
    FakeSession.behaviours = ["refused"] * 30          # refused until... (popped per attempt)
    src, reboots, states = recovery_source(tmp_path, monkeypatch)

    orig = net.reboot_scanner
    def reboot_and_fix(host, port):
        FakeSession.behaviours = []                    # the reboot clears the lockout
        return orig(host, port)
    monkeypatch.setattr(net, "reboot_scanner", reboot_and_fix)

    frames = run_for(src, 10, n=3)
    assert len(frames) == 3 and len(reboots) == 1
    names = [s for s, _ in states]
    assert "rebooting" in names and names[-1] == "streaming"
    info = dict(states)["rebooting"]
    assert info["auto"] and info["acknowledged"] and "control channel" in info["error"]


def test_gives_up_after_max_tries_and_asks_for_a_power_cycle(tmp_path, monkeypatch):
    FakeSession.behaviours = ["refused"] * 10**4
    src, reboots, states = recovery_source(tmp_path, monkeypatch, auto_reboot_max_tries=2)
    run_for(src, 5, until=lambda: any(s == "needs_power_cycle" for s, _ in states))
    run_for(src, 0.4)                                  # and it stays given up
    assert len(reboots) == 2
    assert [s for s, _ in states].count("needs_power_cycle") == 1


def test_no_reboot_during_the_startup_grace_period(tmp_path, monkeypatch):
    FakeSession.behaviours = ["refused"] * 10**4
    src, reboots, _ = recovery_source(tmp_path, monkeypatch)
    src.cfg = _replace(src.cfg, auto_reboot_grace_s=60)
    run_for(src, 0.7)                                  # > 2x the 0.3 s threshold
    assert reboots == []


def test_unreachable_scanner_is_not_rebooted(tmp_path, monkeypatch):
    FakeSession.behaviours = ["fail"] * 10**4          # link down: control doesn't answer
    src, reboots, _ = recovery_source(tmp_path, monkeypatch)
    run_for(src, 0.7)                                  # > 2x the 0.3 s threshold
    assert reboots == []


def test_auto_reboot_can_be_turned_off(tmp_path, monkeypatch):
    FakeSession.behaviours = ["refused"] * 10**4
    src, reboots, _ = recovery_source(tmp_path, monkeypatch, auto_reboot=False)
    run_for(src, 0.7)                                  # > 2x the 0.3 s threshold
    assert reboots == []


def test_admin_reboot_tears_down_first_then_reboots_and_reconnects(tmp_path, monkeypatch):
    src, reboots, states = recovery_source(tmp_path, monkeypatch)

    async def main():
        gen = src.events()
        await gen.__anext__()                          # streaming
        src.request_reboot()
        got = 0
        async with asyncio.timeout(10):
            async for _ in gen:
                if len(FakeSession.instances) >= 2:
                    got += 1
                    if got >= 2:
                        break
        await gen.aclose()
    asyncio.run(main())
    first = FakeSession.instances[0]
    assert first.stopped == 1 and len(reboots) == 1   # TEARDOWN before the reboot
    assert ("rebooting" in [s for s, _ in states])
    assert dict(states)["rebooting"]["auto"] is False


# ---- lockout prevention: parked RTSP connections -----------------------------------------------

import socket as _socket
import threading as _threading


def fake_rtsp_server():
    """A TCP server that accepts one connection and doesn't answer until told to."""
    srv = _socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    box = {}
    def accept():
        box["conn"], _ = srv.accept()
    t = _threading.Thread(target=accept, daemon=True); t.start()
    return srv, box, t


def test_failed_keepalive_parks_the_socket_with_a_teardown_that_goes_out_later(monkeypatch):
    srv, box, t = fake_rtsp_server()
    c = net._ParkingRtspClient("127.0.0.1", port=srv.getsockname()[1], timeout=0.3)
    c.connect(); t.join(2)
    c._session = "ABC123"                              # as after SETUP
    with pytest.raises(Exception):
        c.get_parameter()                              # "link down": no answer → timeout
    assert c._socket is None and len(net.parking) == 1
    # "link back": the scanner reads what was queued and answers both requests
    conn = box["conn"]; conn.settimeout(2)
    data = b""
    while b"TEARDOWN" not in data or not data.endswith(b"\r\n\r\n"):
        data += conn.recv(4096)
    assert b"GET_PARAMETER" in data and b"Session: ABC123" in data
    conn.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\nRTSP/1.0 200 OK\r\nCSeq: 2\r\n\r\n")
    for _ in range(40):
        if len(net.parking) == 0:
            break
        time.sleep(0.1)
    assert len(net.parking) == 0                       # closed once the TEARDOWN was answered
    conn.close(); srv.close()


def test_failed_teardown_is_parked_not_dropped():
    srv, box, t = fake_rtsp_server()
    c = net._ParkingRtspClient("127.0.0.1", port=srv.getsockname()[1], timeout=0.3)
    c.connect(); t.join(2)
    c._session = "S1"
    with pytest.raises(Exception):
        c.teardown()
    assert len(net.parking) == 1
    conn = box["conn"]
    conn.close()                                       # scanner side closes → parked socket freed
    for _ in range(40):
        if len(net.parking) == 0:
            break
        time.sleep(0.1)
    assert len(net.parking) == 0
    srv.close()


def test_no_session_means_no_parking():
    srv, box, t = fake_rtsp_server()
    c = net._ParkingRtspClient("127.0.0.1", port=srv.getsockname()[1], timeout=0.3)
    c.connect(); t.join(2)
    with pytest.raises(Exception):
        c.options()                                    # before SETUP: nothing to tear down
    assert len(net.parking) == 0
    box["conn"].close(); srv.close()


def test_reboot_scanner_sends_msm_and_reads_the_ack():
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM); s.bind(("127.0.0.1", 0))
    got = {}
    def scanner():
        data, addr = s.recvfrom(100)
        got["cmd"] = data
        s.sendto(b"PSI,<XML>,\r", addr)                # unrelated push first
        s.sendto(b"MSM,OK\r", addr)
    th = _threading.Thread(target=scanner, daemon=True); th.start()
    assert net.reboot_scanner("127.0.0.1", s.getsockname()[1]) is True
    assert got["cmd"] == b"MSM,0\r"
    s.close()


def test_failed_request_without_a_session_is_reset_not_half_closed():
    srv, box, t = fake_rtsp_server()
    c = net._ParkingRtspClient("127.0.0.1", port=srv.getsockname()[1], timeout=0.3)
    c.connect(); t.join(2)
    with pytest.raises(Exception):
        c.options()                                    # scanner slow to answer: timeout
    conn = box["conn"]; conn.settimeout(2)
    got = b""
    with pytest.raises(ConnectionResetError):          # RST, not a FIN it could ignore
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break                                  # a FIN would end here instead
            got += chunk
    assert got.startswith(b"OPTIONS")
    conn.close(); srv.close()


def test_waits_for_the_scanner_to_settle_after_a_parked_teardown(tmp_path, monkeypatch):
    monkeypatch.setattr(net, "PARK_SETTLE_S", 0.5)
    monkeypatch.setattr(net.parking, "last_released", time.monotonic())
    src = net.SDS200NetworkSource(ScannerConfig(), tmp_path / "lock")
    t0 = time.monotonic()
    asyncio.run(take(src, 1))
    assert time.monotonic() - t0 >= 0.45


def test_backoff_ends_early_when_a_parked_teardown_is_answered(tmp_path, monkeypatch):
    monkeypatch.setattr(net.parking, "last_released", None)
    src = net.SDS200NetworkSource(ScannerConfig(), tmp_path / "lock")

    async def main():
        async def release_soon():
            await asyncio.sleep(0.3)
            net.parking.last_released = time.monotonic()   # the parking thread does this
        asyncio.ensure_future(release_soon())
        t0 = time.monotonic()
        await src._sleep_until_retry(30)
        return time.monotonic() - t0
    took = asyncio.run(main())
    assert 0.25 < took < 2.0


def test_backoff_runs_its_course_without_a_release(tmp_path, monkeypatch):
    monkeypatch.setattr(net.parking, "last_released", None)
    src = net.SDS200NetworkSource(ScannerConfig(), tmp_path / "lock")
    t0 = time.monotonic()
    asyncio.run(src._sleep_until_retry(0.5))
    assert time.monotonic() - t0 >= 0.45


_REAL_SESSION = net._LiveSession  # the autouse fixture swaps in FakeSession


def test_packets_that_place_no_audio_do_not_count_as_audio():
    # Regression: a timeline that drops every packet must look stale, so capture reconnects
    # instead of reporting "streaming" while recording nothing.
    import threading
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from scanner_transcriber.timeline import RtpTimeline

    s = object.__new__(_REAL_SESSION)
    s.offset, s.timeline, s._anchor = 0, RtpTimeline(), None
    s._lock, s.last_audio_mono = threading.Lock(), None
    s._put = lambda ev: None
    payload = bytes([0xFF]) * 320
    s._on_packet(SimpleNamespace(timestamp=0, payload=payload, observed_at=datetime.now(UTC)))
    first = s.last_audio_mono
    assert first is not None
    s._on_packet(SimpleNamespace(timestamp=0, payload=payload, observed_at=datetime.now(UTC)))
    assert s.last_audio_mono == first  # the repeat placed nothing
