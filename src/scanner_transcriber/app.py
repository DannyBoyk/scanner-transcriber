"""Wires a source → segmenter → clip store, and supervises the transcription worker process."""
from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import signal
import threading
import time
from dataclasses import dataclass

from . import commands, db, prompteval, sdnotify, statestore
from .audio import write_wav
from .clipstore import ClipStore
from .config import Config
from .models import AudioFrame
from .monitoring import CaptureReporter, HealthMonitor
from .segmenter import Segmenter
from .source.base import ScannerSource

log = logging.getLogger(__name__)

HEARTBEAT_S = 5  # capture heartbeat; health "capture down" threshold is 15 s (3 missed beats)
COMMAND_POLL_S = 5  # Admin-page commands (reconnect scanner, restart worker)
RETENTION_FIRST_S = 120  # first retention pass after start (then every run_every_minutes)
SHUTDOWN_WORKER_S = 25  # on shutdown, wait this long for the worker's clip, then kill it
SAVE_RETRY_S = (0.5, 2.0, 5.0)  # a failed clip save is retried after these pauses, then spilled


@dataclass
class RunStats:
    frames: int = 0
    statuses: int = 0
    clips: int = 0
    spilled: int = 0  # clips that couldn't be stored in the DB (WAV kept under data/spill/)


async def run_pipeline(cfg: Config, source: ScannerSource, *,
                       stop: asyncio.Event | None = None) -> RunStats:
    stop = stop or asyncio.Event()
    conn = db.connect(cfg.storage.db_path)
    store = ClipStore(conn, cfg.storage.data_dir, cfg.scanner.id)
    seg = Segmenter(cfg.segmenter)
    stats = RunStats()
    kind = getattr(source, "kind", "live")
    reporter = CaptureReporter(conn, source_kind=kind,
                               source_stats=source if kind == "live" else None)
    if hasattr(source, "on_state"):
        source.on_state = reporter.on_source_state

    async def save_all(clips):
        """A failed save (database locked, disk full) must never stop capture: retry, and as a
        last resort keep the audio under data/spill/ and carry on."""
        for c in clips:
            for wait in (*SAVE_RETRY_S, None):
                try:
                    store.save(c)
                    stats.clips += 1
                    break
                except Exception as e:  # noqa: BLE001
                    if wait is None:
                        stats.spilled += 1
                        _spill(cfg, conn, c, e)
                        break
                    log.warning("saving clip failed (%s: %s); retrying in %.1fs",
                                type(e).__name__, e, wait)
                    await asyncio.sleep(wait)

    async def close_on_stop():
        await stop.wait()
        await source.close()  # unblocks a source waiting on reconnect/backoff

    async def heartbeat():
        """Publish capture state every 5 s and check derived conditions every 10 s, even while
        the source is blocked reconnecting (so a stale heartbeat means the process is gone).
        Connection changes are written immediately by the reporter, not on this timer."""
        n = 0
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            sdnotify.notify("WATCHDOG=1")  # systemd restarts us if the event loop ever hangs
            try:
                reporter.publish()
                if n % 2 == 0:
                    await asyncio.to_thread(_check_health, cfg)
            except Exception:  # noqa: BLE001 — status must never break capture
                log.exception("status publishing failed")
            n += 1

    closer = asyncio.create_task(close_on_stop())
    reporter.started()
    beat = asyncio.create_task(heartbeat())
    sdnotify.notify("READY=1")
    events = source.events()
    reason = "clean shutdown"
    try:
        async for ev in events:
            if isinstance(ev, AudioFrame):
                stats.frames += 1
                reporter.saw_audio(ev)
                await save_all(seg.feed_audio(ev))
            else:
                stats.statuses += 1
                reporter.saw_status(ev)
                await save_all(seg.feed_status(ev))
            if stop.is_set():
                log.info("stopping")
                break
        await save_all(seg.flush())
        if kind == "replay" and not stop.is_set():
            reason = "replay finished"
    except BaseException as e:
        reason = f"error: {type(e).__name__}: {e}"
        raise
    finally:
        closer.cancel()
        beat.cancel()
        await events.aclose()
        await source.close()
        try:
            reporter.stopped(reason)
        except Exception:  # noqa: BLE001 — don't mask the real reason for stopping
            log.exception("could not record capture stop")
        conn.close()
    return stats


def _spill(cfg: Config, conn, clip, err: BaseException) -> None:
    """Keep a clip's audio that couldn't be stored, so nothing heard is silently lost."""
    st = clip.status
    name = (f"{clip.started_at.astimezone():%Y-%m-%d_%H%M%S}"
            f"{'_TG' + st.tgid if st and st.tgid else ''}.wav")
    path = cfg.storage.data_dir / "spill" / name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_wav(path, clip.pcm)
        where = f"audio kept in {path}"
    except Exception as e2:  # noqa: BLE001 — e.g. the disk really is full
        where = f"audio lost too ({type(e2).__name__}: {e2})"
    log.error("could not store a clip (%s: %s); %s", type(err).__name__, err, where)
    try:
        statestore.event(conn, "capture", "error", "clip_not_saved",
                         f"A {clip.duration_ms / 1000:.1f}s transmission couldn't be stored "
                         f"({type(err).__name__}: {err}); {where}")
    except Exception:  # noqa: BLE001
        pass


_monitors: dict[str, HealthMonitor] = {}


def _check_health(cfg: Config) -> None:
    """Runs in a thread (own connection); the monitor keeps its state across calls."""
    conn = db.connect(cfg.storage.db_path)
    try:
        key = str(cfg.storage.db_path)
        mon = _monitors.setdefault(key, HealthMonitor(conn, cfg))
        mon.conn = conn
        mon.check()
    finally:
        conn.close()


class WorkerSupervisor:
    """Runs the transcription worker in its own process; restarts it if it dies.

    `watch()` (event loop) and `restart()` (Admin command, in a thread) can overlap, so every
    start/stop happens under one lock, and `watch()` only replaces the process it saw die."""

    HEALTHY_S = 600  # a worker that ran this long before dying resets the crash backoff
    POLL_S = 2.0     # how often watch() checks the worker
    BACKOFF_S = 1.0  # first restart delay; doubles per crash, up to 60 s

    def __init__(self, cfg: Config, log_level: int = logging.INFO) -> None:
        self.cfg = cfg
        self.log_level = log_level
        self.proc: mp.process.BaseProcess | None = None
        self.restarts = 0
        self._started_at = 0.0
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            self._start()

    def _start(self) -> None:
        from .worker import worker_process_main

        ctx = mp.get_context("spawn")
        self.proc = ctx.Process(target=worker_process_main, args=(self.cfg, self.log_level),
                                name="transcriber")
        self.proc.start()
        self._started_at = time.monotonic()
        log.info("worker process started (pid %s)", self.proc.pid)

    def _replace_if_current(self, dead) -> bool:
        """Start a new worker unless someone (Admin restart) already replaced `dead`."""
        with self._lock:
            if self.proc is not dead or self.proc.is_alive():
                return False
            self._start()
            return True

    async def watch(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.POLL_S)
            except TimeoutError:
                pass
            dead = self.proc
            if stop.is_set() or dead is None or dead.is_alive():
                continue
            if time.monotonic() - self._started_at >= self.HEALTHY_S:
                self.restarts = 0
            self.restarts += 1
            delay = min(60, self.BACKOFF_S * 2 ** min(self.restarts - 1, 6))
            log.error("worker exited (code %s); restarting in %.0fs", dead.exitcode, delay)
            self._event("error", "crashed", f"Transcription worker crashed (exit code "
                        f"{dead.exitcode}); restarting in {delay:.0f}s")
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                if await asyncio.to_thread(self._replace_if_current, dead):
                    self._event("info", "restarted", "Transcription worker restarted")

    def _event(self, level: str, kind: str, message: str) -> None:
        try:
            conn = db.connect(self.cfg.storage.db_path)
            try:
                statestore.event(conn, "worker", level, kind, message)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            log.exception("could not record worker event")

    def restart(self, timeout: float = 60) -> str:
        with self._lock:
            self._stop(timeout)
            self._start()
            return f"worker restarted (pid {self.proc.pid})"

    def stop(self, timeout: float = 60) -> None:
        with self._lock:
            self._stop(timeout)

    def _stop(self, timeout: float) -> None:
        """SIGTERM lets the worker finish its current clip; kill only if it hangs."""
        if self.proc is None or not self.proc.is_alive():
            return
        self.proc.terminate()
        self.proc.join(timeout)
        if self.proc.is_alive():
            log.warning("worker did not stop in %.0fs; killing", timeout)
            self.proc.kill()
            self.proc.join(5)


def _queue_empty(cfg: Config) -> bool:
    conn = db.connect(cfg.storage.db_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM transmissions "
                         "WHERE status IN ('queued', 'processing')").fetchone()[0]
    finally:
        conn.close()
    return n == 0


def _clean_start_once(cfg: Config):
    from . import cleanstart
    conn = db.connect(cfg.storage.db_path)
    try:
        return cleanstart.run(conn, cfg)
    finally:
        conn.close()


def _retention_once(cfg: Config) -> None:
    from . import retention
    conn = db.connect(cfg.storage.db_path)
    try:
        retention.run(conn, cfg)
    finally:
        conn.close()


async def handle_commands(cfg: Config, source, sup: WorkerSupervisor | None) -> None:
    """Act on Admin-page commands addressed to this process."""
    conn = db.connect(cfg.storage.db_path)
    try:
        for cmd in commands.take(conn, "capture"):
            if cmd["action"] == "clean-start":
                conn.commit()
                summary = await asyncio.to_thread(_clean_start_once, cfg)  # off the event loop
                commands.done(conn, cmd, summary.text())
            elif cmd["action"] == "reboot" and hasattr(source, "request_reboot"):
                source.request_reboot()
                commands.done(conn, cmd, "rebooting the scanner (audio back in about 30 s)")
            elif cmd["action"] == "reconnect" and hasattr(source, "request_reconnect"):
                source.request_reconnect()
                commands.done(conn, cmd, "reconnecting now")
            else:
                commands.done(conn, cmd, "not a live scanner source (replay)", "warn")
        for cmd in commands.take(conn, "worker", {"restart"}):
            if sup is None:
                commands.done(conn, cmd, "the worker isn't run by this process", "warn")
            else:
                commands.done(conn, cmd, await asyncio.to_thread(sup.restart))
        prompteval.fail_expired(conn)   # take() just expired commands nobody picked up
        conn.commit()
    finally:
        conn.close()


def run(cfg: Config, source: ScannerSource, *, with_worker: bool = True,
        drain: bool = False) -> RunStats:
    """Capture until stopped (Ctrl-C/SIGTERM) or the source ends. With `drain`, keep the
    worker going after the source ends until every queued clip is transcribed."""
    sup = WorkerSupervisor(cfg, logging.getLogger().level) if with_worker else None

    async def main() -> RunStats:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        watcher = None
        if sup:
            db.connect(cfg.storage.db_path).close()  # apply migrations before either side uses it
            sup.start()
            watcher = asyncio.create_task(sup.watch(stop))
        cmd_task = asyncio.create_task(command_loop(stop))
        ret_task = asyncio.create_task(retention_loop(stop))
        try:
            stats = await run_pipeline(cfg, source, stop=stop)
            if sup and drain and not stop.is_set():
                log.info("source finished; waiting for the worker to drain the queue")
                while not stop.is_set() and not await asyncio.to_thread(_queue_empty, cfg):
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2)
                    except TimeoutError:
                        pass
            return stats
        finally:
            stop.set()
            sdnotify.notify("STOPPING=1")
            cmd_task.cancel()
            ret_task.cancel()
            if watcher:
                await watcher
            if sup:
                # the worker finishes its current clip; systemd's TimeoutStopSec is 45 s
                await asyncio.to_thread(sup.stop, SHUTDOWN_WORKER_S)

    async def command_loop(stop: asyncio.Event) -> None:
        """Admin-page commands for this process: capture/reconnect, worker/restart."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=COMMAND_POLL_S)
            except TimeoutError:
                pass
            if stop.is_set():
                return
            try:
                await handle_commands(cfg, source, sup)
            except Exception:  # noqa: BLE001 — never let this take capture down
                log.exception("command handling failed")

    async def retention_loop(stop: asyncio.Event) -> None:
        """Keep the disk bounded: first pass shortly after start, then every N minutes."""
        delay = RETENTION_FIRST_S
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                await asyncio.to_thread(_retention_once, cfg)
            except Exception:  # noqa: BLE001 — never let this take capture down
                log.exception("retention failed")
            delay = cfg.retention.run_every_minutes * 60

    stats = asyncio.run(main())
    log.info("done: %d clips from %d frames (%.0f s audio), %d statuses",
             stats.clips, stats.frames, stats.frames * 0.04, stats.statuses)
    return stats
