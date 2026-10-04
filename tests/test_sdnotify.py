"""systemd notify: READY / WATCHDOG / STOPPING reach NOTIFY_SOCKET; no-op without it."""
import asyncio
import socket
from dataclasses import replace

from scanner_transcriber import app, sdnotify
from scanner_transcriber.config import Config, StorageConfig
from scanner_transcriber.source.replay import ReplaySource

from segsim import Sim
from test_replay_app import write_session


def listen(tmp_path, monkeypatch):
    path = str(tmp_path / "notify.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(path)
    s.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    return s


def drain(s):
    out = []
    while True:
        try:
            out.append(s.recv(4096).decode())
        except BlockingIOError:
            return out


def test_noop_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert sdnotify.notify("READY=1") is False


def test_run_reports_ready_and_pings_the_watchdog(tmp_path, monkeypatch):
    s = listen(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "HEARTBEAT_S", 0.05)
    sim = Sim(3).status(0.2, "Mute").ptt(1.0, 2.0, "1101", unit="UID:1")
    write_session(tmp_path / "session", sim)
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path / "data"))

    class Slow(ReplaySource):          # stretch the replay so a few heartbeats happen
        async def events(self):
            async for ev in super().events():
                await asyncio.sleep(0.002)
                yield ev

    asyncio.run(app.run_pipeline(cfg, Slow(tmp_path / "session")))
    msgs = drain(s)
    assert msgs[0] == "READY=1" and msgs.count("WATCHDOG=1") >= 2
