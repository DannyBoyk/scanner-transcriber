"""WorkerSupervisor: crash restarts with backoff, and Admin restarts that overlap them."""
import asyncio
import time
from dataclasses import replace

import pytest

from scanner_transcriber import app as app_mod
from scanner_transcriber.app import WorkerSupervisor
from scanner_transcriber.config import Config, StorageConfig


class FakeProc:
    made: list["FakeProc"] = []

    def __init__(self, *a, **k):
        self.pid = len(FakeProc.made) + 1
        self.alive, self.exitcode, self.die_at = False, None, None
        FakeProc.made.append(self)

    def start(self):
        self.alive = True

    def is_alive(self):
        if self.alive and self.die_at and time.monotonic() >= self.die_at:
            self.alive, self.exitcode = False, 0
        return self.alive

    def crash(self):
        self.alive, self.exitcode = False, 1

    def terminate(self):  # SIGTERM: exits after its current clip
        self.die_at = time.monotonic() + 0.05

    def join(self, timeout=None):
        while self.is_alive():
            time.sleep(0.02)

    def kill(self):
        self.alive = False


@pytest.fixture
def sup(tmp_path, monkeypatch):
    FakeProc.made = []

    class Ctx:
        Process = FakeProc
    monkeypatch.setattr(app_mod.mp, "get_context", lambda _: Ctx)
    # real timing scaled down 20-40x: poll 2 s → 0.05 s, first backoff 1 s → 0.1 s
    monkeypatch.setattr(WorkerSupervisor, "POLL_S", 0.05)
    monkeypatch.setattr(WorkerSupervisor, "BACKOFF_S", 0.1)
    return WorkerSupervisor(replace(Config(), storage=StorageConfig(data_dir=tmp_path)))


def alive():
    return [p for p in FakeProc.made if p.is_alive()]


def test_admin_restart_during_crash_backoff_leaves_one_worker(sup):
    async def main():
        stop = asyncio.Event()
        sup.start()
        sup.restarts = 2                          # crashed before: next backoff is 0.4 s
        w = asyncio.create_task(sup.watch(stop))
        sup.proc.crash()
        await asyncio.sleep(0.2)                  # watch() noticed and is waiting out its backoff
        await asyncio.to_thread(sup.restart)      # Admin: "Restart worker"
        await asyncio.sleep(0.6)                  # the backoff ends
        stop.set()
        await w
    asyncio.run(main())
    assert len(FakeProc.made) == 2                # watch() saw it was already replaced
    assert alive() == [sup.proc]


def test_crash_is_restarted_after_backoff(sup):
    async def main():
        stop = asyncio.Event()
        sup.start()
        w = asyncio.create_task(sup.watch(stop))
        sup.proc.crash()
        await asyncio.sleep(0.4)                  # 0.05 s poll + 0.1 s first backoff
        stop.set()
        await w
    asyncio.run(main())
    assert len(FakeProc.made) == 2 and alive() == [sup.proc]


def test_backoff_resets_after_a_healthy_run(sup, monkeypatch):
    monkeypatch.setattr(WorkerSupervisor, "HEALTHY_S", 0.3)

    async def main():
        stop = asyncio.Event()
        sup.start()
        sup.restarts = 6                          # long crash history: would wait 6.4 s
        w = asyncio.create_task(sup.watch(stop))
        await asyncio.sleep(0.5)                  # ran "healthily"
        sup.proc.crash()
        await asyncio.sleep(0.4)                  # reset → first backoff (0.1 s) again
        stop.set()
        await w
    asyncio.run(main())
    assert sup.restarts == 1 and len(FakeProc.made) == 2


def test_restart_stops_the_old_worker_first(sup):
    sup.start()
    first = sup.proc
    sup.restart()
    assert not first.is_alive() and alive() == [sup.proc]
