"""Thin adapters over the `sds200` library (MIT, https://pypi.org/project/sds200/)."""
from __future__ import annotations

import asyncio
import atexit
import fcntl
import logging
import os
import select
import socket
import struct
import threading
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sds200 import SDSScanner
from sds200.exceptions import SDS200Error
from sds200.models import ScannerInfo
from sds200.network_audio import NetworkAudioTransport
from sds200.rtsp import RtspClient

from ..audio import SCANNER_RATE, ulaw_to_pcm16
from ..config import ScannerConfig
from ..models import AudioFrame, ScannerStatus
from ..timeline import RtpTimeline
from .base import SourceEvent

log = logging.getLogger(__name__)


def _attr(info: ScannerInfo, tag: str, name: str) -> str | None:
    node = info.node(tag)
    if node is None:
        return None
    value = node.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _parse_freq_hz(value: str | None) -> int | None:
    if not value:
        return None
    v = value.strip().upper()
    try:
        if v.endswith("MHZ"):
            return round(float(v[:-3]) * 1_000_000)
        return int(float(v))
    except ValueError:
        return None


def _parse_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def summarize_status(info: ScannerInfo) -> dict[str, Any]:
    """Normalize a GSI/PSI payload to the fields ScannerStatus will need.

    Field names verified against SDS200 fw 1.21.00 GSI output (P25 trunk scan).
    Raw XML is kept alongside so nothing is lost while mappings are refined.
    """
    tgid_raw = _attr(info, "TGID", "TGID")  # e.g. "TGID: ---" when idle, "TGID:1234" when active
    tgid = None
    if tgid_raw:
        tgid = tgid_raw.split(":", 1)[-1].strip()
        if not tgid or set(tgid) <= {"-"}:
            tgid = None
    return {
        "mode": info.mode,
        "system": _attr(info, "System", "Name"),
        "system_type": _attr(info, "System", "SystemType"),
        "department": _attr(info, "Department", "Name"),
        "channel": _attr(info, "TGID", "Name"),
        "tgid": tgid,
        "unit_id": _attr(info, "UnitID", "U_Id"),
        "unit_name": _attr(info, "UnitID", "Name"),
        "site": _attr(info, "Site", "Name"),
        "frequency_hz": _parse_freq_hz(_attr(info, "SiteFrequency", "Freq")),
        "modulation": _attr(info, "Site", "Mod"),
        "mute": _attr(info, "Property", "Mute"),
        "p25_status": _attr(info, "Property", "P25Status"),
        "sig": _parse_int(_attr(info, "Property", "Sig")),
        "rssi": _parse_int(_attr(info, "Property", "Rssi")),
        "volume": _parse_int(_attr(info, "Property", "VOL")),
        "overwrite": _attr(info, "OverWrite", "Text"),
    }


class AudioSessionLock:
    """Process-wide exclusive lock: only one owner of the scanner's single RTSP session (R2)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def __enter__(self) -> "AudioSessionLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise RuntimeError(
                f"Another process holds the scanner audio session ({self.path})."
            ) from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd
        return self

    def __exit__(self, *_: object) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


# ---- lockout prevention: park dead RTSP connections -------------------------------------------
#
# The SDS200 keeps an audio session until it hears TEARDOWN (or the connection's FIN/RST); while
# it has one, it refuses new connections on 554. If the link drops, our TEARDOWN/keepalive times
# out and the library closes the socket; the kernel then retries the FIN for only ~50 s, so an
# outage longer than that leaves the scanner locked (docs/sds200-rtsp-lockout.md). Instead of
# closing, a failed connection is *parked* with a TEARDOWN queued on it: the kernel keeps
# retransmitting it (tcp_retries2, ~15 min) and it reaches the scanner as soon as the link is
# back. A background thread closes parked sockets once the scanner answers or TCP gives up.

PARK_MAX_S = 20 * 60
PARK_POLL_S = 1.0      # how often the parking lot checks its sockets
PARK_SETTLE_S = 5.0    # after the scanner answers a parked TEARDOWN, let its RTSP server settle
RTSP_TIMEOUT_S = 10.0  # the scanner can be slow to answer right after releasing a session


class _ParkingLot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._parked: list[dict] = []
        self._thread: threading.Thread | None = None
        self.last_released: float | None = None   # when a parked TEARDOWN was last answered

    def park(self, stream, teardown_cseq: int | None) -> None:
        try:
            stream.setblocking(False)
        except OSError:
            pass
        with self._lock:
            self._parked.append({"sock": stream, "since": time.monotonic(), "buf": b"",
                                 "want": f"CSeq: {teardown_cseq}\r\n".encode()
                                 if teardown_cseq else None})
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="rtsp-parking",
                                                daemon=True)
                self._thread.start()
        log.warning("RTSP connection parked with a TEARDOWN queued (link down?); it goes out "
                    "when the scanner is reachable again")

    def __len__(self) -> int:
        with self._lock:
            return len(self._parked)

    def _run(self) -> None:
        while True:
            with self._lock:
                items = list(self._parked)
            if not items:
                return
            for it in items:
                done, why = self._check(it)
                if done:
                    try:
                        it["sock"].close()
                    except OSError:
                        pass
                    with self._lock:
                        self._parked.remove(it)
                        self.last_released = time.monotonic()
                    log.info("parked RTSP connection closed after %.0fs: %s",
                             time.monotonic() - it["since"], why)
            time.sleep(PARK_POLL_S)

    @staticmethod
    def _check(it) -> tuple[bool, str]:
        if time.monotonic() - it["since"] > PARK_MAX_S:
            return True, "gave up waiting"
        sock = it["sock"]
        try:
            ready, _, _ = select.select([sock], [], [], 0)
            if not ready:
                return False, ""
            data = sock.recv(4096)
        except BlockingIOError:
            return False, ""
        except OSError as e:
            return True, f"connection failed ({e.strerror or e})"
        if not data:
            return True, "scanner closed the connection"
        it["buf"] = (it["buf"] + data)[-8192:]
        if it["want"] is None or it["want"] in it["buf"]:
            return True, "scanner answered the TEARDOWN"
        return False, ""


parking = _ParkingLot()


class _ParkingRtspClient(RtspClient):
    """RtspClient whose socket isn't thrown away when a request fails mid-flight."""

    _in_request: str | None = None

    def _request(self, method, uri, **kw):
        self._in_request = method
        try:
            return super()._request(method, uri, **kw)
        finally:
            self._in_request = None

    def close(self) -> None:
        stream = self._socket
        # only with a session to end: a bare connection would just hold the scanner's one slot
        if self._in_request is not None and stream is not None and self._session:
            cseq = None
            if self._in_request == "TEARDOWN":
                cseq = self._cseq                 # already in the send queue
            else:
                cseq = self._cseq + 1             # queue one behind the failed request
                payload = (f"TEARDOWN {self._aggregate_uri}/ RTSP/1.0\r\nCSeq: {cseq}\r\n"
                           f"User-Agent: {self.user_agent}\r\nSession: {self._session}\r\n\r\n")
                try:
                    stream.settimeout(1.0)
                    stream.sendall(payload.encode("ascii"))
                except OSError:
                    cseq = None
            self._socket = None
            parking.park(stream, cseq)
        elif stream is not None and (self._in_request is not None or not self._session):
            # No session to keep: abort with a TCP reset. A normal close (FIN) after a failed
            # request can leave the scanner holding the connection half-open, and with it the
            # single audio slot (seen after a slow first reply, 2026-09-30).
            try:
                stream.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass
        super().close()


def _parking_client_factory(host: str, port: int, path: str, timeout: float) -> RtspClient:
    return _ParkingRtspClient(host, port=port, path=path, timeout=timeout)


# ---- lockout recovery: reboot the scanner -------------------------------------------------------

def reboot_scanner(host: str, port: int, timeout: float = 3.0) -> bool:
    """Send MSM,0 (undocumented; reboots the SDS200, confirmed on fw 1.21.00). True if the
    scanner acknowledged with MSM,OK."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(b"MSM,0\r", (host, port))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, _ = s.recvfrom(65535)
            except TimeoutError:
                break
            if data.startswith(b"MSM,"):
                return data.strip() == b"MSM,OK"
    return False


# ---- live source ---------------------------------------------------------------------------

class SourceStale(Exception):
    """No audio or no status for too long: reconnect."""


class ReconnectRequested(Exception):
    """Reconnect asked for from the Admin page (no backoff)."""


class RebootRequested(Exception):
    """Scanner reboot asked for from the Admin page."""


class _LiveSession:
    """One connection: PSI push over UDP + RTSP/RTP audio. Callbacks run on library threads
    and hand events to the asyncio loop. `stop()` always sends RTSP TEARDOWN."""

    def __init__(self, cfg: ScannerConfig, loop: asyncio.AbstractEventLoop,
                 queue: asyncio.Queue, sample_offset: int) -> None:
        self.cfg = cfg
        self.loop = loop
        self.queue = queue
        self.offset = sample_offset
        self.timeline = RtpTimeline()
        self.last_audio_mono: float | None = None
        self.last_status_mono: float | None = None
        self._anchor: tuple[float, int] | None = None  # (arrival mono, sample at packet end)
        self._lock = threading.Lock()
        self._stopped = False
        self.phase = "control"   # control → audio (control answered) → streaming
        self.audio = NetworkAudioTransport(cfg.host, rtsp_port=cfg.rtsp_port,
                                           rtsp_timeout=RTSP_TIMEOUT_S,
                                           rtsp_client_factory=_parking_client_factory)
        self.scanner = SDSScanner.network(cfg.host, remote_port=cfg.control_port)

    @property
    def end_sample(self) -> int:
        return self.offset + self.timeline.samples

    def _put(self, ev) -> None:
        self.loop.call_soon_threadsafe(self.queue.put_nowait, ev)

    def _on_packet(self, pkt) -> None:
        mono = time.monotonic()
        with self._lock:
            start, pcm = self.timeline.place(pkt.timestamp, ulaw_to_pcm16(pkt.payload))
            end = self.offset + self.timeline.samples
            self._anchor = (mono, end)
            self.last_audio_mono = mono
        if pcm.size:
            wall = pkt.observed_at - timedelta(seconds=pcm.size / SCANNER_RATE)
            self._put(AudioFrame(self.offset + start, pcm, wall))

    def _on_psi(self, msg) -> None:
        if not isinstance(msg, ScannerInfo):
            return  # PSI ack packets
        mono = time.monotonic()
        with self._lock:
            sample = (None if self._anchor is None
                      else self._anchor[1] + (mono - self._anchor[0]) * SCANNER_RATE)
            self.last_status_mono = mono
        self._put(ScannerStatus.from_summary(summarize_status(msg), mono=mono,
                                             wall=msg.received_at, sample=sample,
                                             raw_xml=msg.raw_xml))

    def start(self) -> None:
        self.scanner.connect()
        log.info("scanner %s firmware %s", self.scanner.get_model(), self.scanner.get_firmware())
        self.scanner.on_psi(self._on_psi)
        self.scanner.start_scanner_info_push(self.cfg.psi_interval_ms)
        self.phase = "audio"
        self.audio.on_packet(self._on_packet)
        self.audio.start(lambda _chunk: None)
        self.phase = "streaming"
        log.info("audio session started (%s)", self.audio.endpoint)

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        try:
            self.audio.stop()  # sends RTSP TEARDOWN if a session was established
            st = self.audio.statistics
            log.info("audio session stopped: teardowns_sent=%d packets=%d lost=%d",
                     st.teardowns_sent, st.packets_delivered, st.packets_lost)
        except Exception:
            log.exception("audio stop failed")
        try:
            self.scanner.stop_scanner_info_push()
        except Exception:
            log.debug("stopping PSI failed", exc_info=True)
        try:
            self.scanner.close()
        except Exception:
            log.debug("scanner close failed", exc_info=True)


class SDS200NetworkSource:
    """Live scanner source with reconnect/backoff. Holds the single-owner audio lock for
    its whole life and guarantees TEARDOWN on every exit path (design R2).

    Audio lockout recovery (docs/sds200-rtsp-lockout.md): while the control channel answers
    but audio can't be started for `auto_reboot_after_s`, the scanner is rebooted with MSM,0
    (limits in ScannerConfig). The Admin page can also ask for a reboot.

    `on_state(state, **info)` (optional) is told about connection changes, for the status
    page: connecting | streaming | reconnecting | session_busy | rebooting |
    needs_power_cycle."""

    kind = "live"
    REBOOT_SETTLE_S = 20  # give the scanner time to boot before connecting again

    def __init__(self, cfg: ScannerConfig, lock_path: Path, *, stale_s: float = 5.0,
                 backoff_max_s: float = 60.0, busy_retry_s: float = 30.0,
                 on_state: Callable[..., None] | None = None) -> None:
        self.cfg = cfg
        self.lock_path = lock_path
        self.stale_s = stale_s
        self.backoff_max_s = backoff_max_s
        self.busy_retry_s = busy_retry_s
        self.on_state = on_state
        self._closed = asyncio.Event()
        self._wake = asyncio.Event()
        self.session: _LiveSession | None = None
        self.reconnects = 0
        self.packets_lost = 0  # across all sessions
        self.reboots = 0
        self._reconnect_requested = False
        self._reboot_requested = False
        self._audio_down_since: float | None = None   # control answers, audio doesn't
        self._last_reboot: float | None = None
        self._auto_tries = 0                          # automatic reboots since audio last worked
        self._gave_up = False

    def request_reconnect(self) -> None:
        """Admin page: end the current audio session (TEARDOWN) and reconnect right away."""
        self._reconnect_requested = True
        self._wake.set()

    def request_reboot(self) -> None:
        """Admin page: end the audio session (TEARDOWN) and reboot the scanner (MSM,0)."""
        self._reboot_requested = True
        self._wake.set()

    def _report(self, state: str, **info) -> None:
        if self.on_state is not None:
            try:
                self.on_state(state, **info)
            except Exception:  # noqa: BLE001 — status reporting must never break capture
                log.exception("status callback failed")

    async def _sleep(self, seconds: float) -> None:
        """Wait, unless closed or woken by an Admin request."""
        if seconds <= 0:
            return
        self._wake.clear()
        closed = asyncio.ensure_future(self._closed.wait())
        woken = asyncio.ensure_future(self._wake.wait())
        try:
            await asyncio.wait({closed, woken}, timeout=seconds,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            closed.cancel()
            woken.cancel()

    async def _sleep_until_retry(self, seconds: float) -> None:
        """Backoff wait that also ends as soon as a parked TEARDOWN is answered: the scanner
        has just released its session, so connect now (after the settle) instead of waiting
        for the next scheduled retry (up to 60 s)."""
        end = time.monotonic() + seconds
        released = parking.last_released
        while not self._closed.is_set():
            left = end - time.monotonic()
            if left <= 0 or parking.last_released != released:
                return
            self._wake.clear()
            await self._sleep(min(0.25, left))
            if self._wake.is_set():
                return

    async def _acquire_lock(self) -> AudioSessionLock | None:
        """Wait (reporting session_busy) while another local process owns the audio session."""
        while not self._closed.is_set():
            lock = AudioSessionLock(self.lock_path)
            try:
                return lock.__enter__()
            except RuntimeError as e:
                retry = datetime.now(UTC) + timedelta(seconds=self.busy_retry_s)
                log.warning("%s; retrying in %.0fs", e, self.busy_retry_s)
                self._report("session_busy", error="another scanner_transcriber process "
                             "(e.g. record) holds the audio session", next_retry_at=retry)
                await self._sleep(self.busy_retry_s)
        return None

    # -- lockout recovery ---------------------------------------------------------------------
    def _auto_reboot_due(self, now: float, started: float) -> bool:
        c = self.cfg
        return (c.auto_reboot and not self._gave_up
                and self._audio_down_since is not None
                and now - self._audio_down_since >= c.auto_reboot_after_s
                and now - started >= c.auto_reboot_grace_s
                and (self._last_reboot is None
                     or now - self._last_reboot >= c.auto_reboot_min_interval_s)
                and len(parking) == 0)          # a parked TEARDOWN may still free it

    async def _reboot(self, why: str, *, auto: bool) -> None:
        self._last_reboot = time.monotonic()
        self.reboots += 1
        if auto:
            self._auto_tries += 1
        log.warning("rebooting the scanner (MSM,0): %s", why)
        try:
            ok = await asyncio.to_thread(reboot_scanner, self.cfg.host, self.cfg.control_port)
        except OSError as e:
            ok = False
            log.warning("sending MSM,0 failed: %s", e)
        retry = datetime.now(UTC) + timedelta(seconds=self.REBOOT_SETTLE_S)
        self._report("rebooting", error=why, acknowledged=ok, auto=auto, next_retry_at=retry)
        self._audio_down_since = None
        await self._sleep(self.REBOOT_SETTLE_S)

    async def events(self) -> AsyncIterator[SourceEvent]:
        loop = asyncio.get_running_loop()
        failures = 0
        next_offset = 0
        begun = time.monotonic()
        lock = await self._acquire_lock()
        if lock is None:
            return
        try:
            while not self._closed.is_set():
                if self._reboot_requested:
                    self._reboot_requested = False
                    self._gave_up, self._auto_tries = False, 0
                    await self._reboot("requested from the Admin page", auto=False)
                    failures = 0
                    continue
                if parking.last_released is not None:
                    await self._sleep(PARK_SETTLE_S - (time.monotonic() - parking.last_released))
                queue: asyncio.Queue = asyncio.Queue()
                session = self.session = _LiveSession(self.cfg, loop, queue, next_offset)
                atexit.register(session.stop)
                self._report("connecting")
                retry = None
                try:
                    await asyncio.to_thread(session.start)
                    failures = 0
                    self._audio_down_since = None
                    self._auto_tries, self._gave_up = 0, False
                    self._report("streaming")
                    started = time.monotonic()
                    while not self._closed.is_set():
                        try:
                            yield await asyncio.wait_for(queue.get(), timeout=1.0)
                        except TimeoutError:
                            pass
                        if self._reconnect_requested:
                            self._reconnect_requested = False
                            raise ReconnectRequested("reconnect requested from the Admin page")
                        if self._reboot_requested:
                            raise RebootRequested("reboot requested from the Admin page")
                        now = time.monotonic()
                        audio_age = now - (session.last_audio_mono or started)
                        status_age = now - (session.last_status_mono or started)
                        if audio_age > self.stale_s or status_age > self.stale_s:
                            if status_age <= self.stale_s:
                                session.phase = "audio"   # status fine, audio gone: RTSP trouble
                            raise SourceStale(f"no audio for {audio_age:.1f}s / "
                                              f"no status for {status_age:.1f}s")
                except (ReconnectRequested, RebootRequested) as e:
                    log.info("%s", e)
                    self.reconnects += 1
                    failures = 0
                    if isinstance(e, ReconnectRequested):
                        self._report("reconnecting", error=str(e), next_retry_at=datetime.now(UTC))
                except Exception as e:  # noqa: BLE001 — network, protocol, even library bugs: retry
                    failures += 1
                    self.reconnects += 1
                    now = time.monotonic()
                    if session.phase == "audio":      # control channel answered, audio didn't
                        self._audio_down_since = self._audio_down_since or now
                    else:
                        self._audio_down_since = None
                    busy = _rtsp_refused(e)
                    delay = (self.busy_retry_s if busy
                             else min(self.backoff_max_s, 2.0 ** (failures - 1)))
                    if isinstance(e, (SourceStale, OSError, SDS200Error)):
                        log.warning("scanner connection problem (%s: %s); retrying in %.0fs",
                                    type(e).__name__, e, delay)
                    else:
                        log.exception("unexpected error from the scanner connection; "
                                      "retrying in %.0fs", delay)
                    retry = datetime.now(UTC) + timedelta(seconds=delay)
                    if busy:
                        self._report("session_busy", error=f"scanner refused audio ({busy})",
                                     next_retry_at=retry)
                    else:
                        self._report("reconnecting", error=_describe(e), next_retry_at=retry)
                finally:
                    await asyncio.to_thread(session.stop)
                    atexit.unregister(session.stop)
                    next_offset = session.end_sample
                    self.packets_lost += getattr(getattr(getattr(session, "audio", None),
                                                         "statistics", None), "packets_lost", 0)
                    self.session = None
                if not failures or self._closed.is_set():
                    continue
                now = time.monotonic()
                if self._auto_reboot_due(now, begun):
                    if self._auto_tries >= self.cfg.auto_reboot_max_tries:
                        self._gave_up = True
                        log.error("scanner audio still unavailable after %d reboots; needs a "
                                  "power cycle", self._auto_tries)
                        self._report("needs_power_cycle", error=(
                            f"audio still refused after {self._auto_tries} automatic reboots"))
                    else:
                        down = now - (self._audio_down_since or now)
                        await self._reboot(f"audio unavailable for {down / 60:.0f} min while the "
                                           "scanner's control channel answers", auto=True)
                        failures = 0
                        continue
                await self._sleep_until_retry((retry - datetime.now(UTC)).total_seconds()
                                              if retry else 1)
        finally:
            lock.__exit__(None, None, None)

    async def close(self) -> None:
        self._closed.set()


def _rtsp_refused(e: BaseException) -> str | None:
    """If the scanner rejected the RTSP request (e.g. another app holds its single audio
    session), return a short description."""
    from sds200.rtsp import RtspStatusError

    seen = set()
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, RtspStatusError):
            return f"RTSP {e.method} {e.status_code} {e.reason}".strip()
        e = e.__cause__ or e.__context__
    return None


def _describe(e: BaseException) -> str:
    if isinstance(e, SourceStale):
        return str(e)
    root = e
    while root.__cause__ is not None:
        root = root.__cause__
    if root is e or not str(root):   # e.g. queue.Empty under "Timed out waiting for MDL"
        return f"{type(e).__name__}: {e}"
    return f"{type(root).__name__}: {root}"
