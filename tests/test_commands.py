"""Admin commands: queue semantics, reconnect without backoff, handling in `run`, the page."""
import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from scanner_transcriber import commands, statestore
from scanner_transcriber.app import handle_commands
from scanner_transcriber.config import Config, ScannerConfig, StorageConfig
from scanner_transcriber.source import sds200_network as net

from test_live_source import FakeSession


def test_post_take_done_and_expire(conn):
    a = commands.post(conn, "capture", "reconnect")
    commands.post(conn, "worker", "restart")
    with pytest.raises(ValueError):
        commands.post(conn, "capture", "format-disk")
    [c] = commands.take(conn, "capture")
    assert c["id"] == a and commands.take(conn, "capture") == []            # taken once
    commands.done(conn, c, "reconnecting now")
    assert statestore.recent_events(conn)[0]["kind"] == "command_reconnect"
    old = commands.post(conn, "capture", "reconnect")
    conn.execute("UPDATE commands SET created_at = ? WHERE id = ?",
                 ((datetime.now(UTC) - timedelta(minutes=11)).isoformat(), old))
    assert commands.take(conn, "capture") == []
    assert conn.execute("SELECT result FROM commands WHERE id = ?", (old,)).fetchone()[0].startswith("expired")


def test_reconnect_request_restarts_session_without_backoff(tmp_path, monkeypatch):
    monkeypatch.setattr(net, "_LiveSession", FakeSession)
    FakeSession.instances, FakeSession.behaviours = [], []
    states = []
    src = net.SDS200NetworkSource(ScannerConfig(), tmp_path / "lock", backoff_max_s=30,
                                  on_state=lambda s, **i: states.append((s, i)))

    async def go():
        gen = src.events()
        await gen.__anext__()
        src.request_reconnect()
        t0 = asyncio.get_running_loop().time()
        while len(FakeSession.instances) < 2:
            await gen.__anext__()
        elapsed = asyncio.get_running_loop().time() - t0
        await gen.aclose()
        return elapsed

    elapsed = asyncio.run(go())
    first, second = FakeSession.instances
    assert first.stopped == 1 and elapsed < 3                  # TEARDOWN, then straight back
    assert any(s == "reconnecting" and "Admin" in i.get("error", "") for s, i in states)


class FakeSup:
    def __init__(self):
        self.restarts = 0

    def restart(self):
        self.restarts += 1
        return "worker restarted (pid 123)"


class LiveLike:
    def __init__(self):
        self.asked = 0

    def request_reconnect(self):
        self.asked += 1

    def request_reboot(self):
        self.rebooted = True


def test_handle_commands(tmp_path, conn):
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path))
    commands.post(conn, "capture", "reconnect")
    commands.post(conn, "worker", "restart")
    commands.post(conn, "worker", "prompt-test")                   # for the worker, not `run`
    conn.commit()
    src, sup = LiveLike(), FakeSup()
    asyncio.run(handle_commands(cfg, src, sup))
    assert src.asked == 1 and sup.restarts == 1
    results = dict(conn.execute("SELECT action, result FROM commands").fetchall())
    assert results["reconnect"] == "reconnecting now" and "restarted" in results["restart"]
    assert results["prompt-test"] is None                          # left for the worker
    commands.post(conn, "capture", "reconnect")
    conn.commit()
    asyncio.run(handle_commands(cfg, object(), None))              # replay source, no worker
    assert "replay" in conn.execute("SELECT result FROM commands ORDER BY id DESC").fetchone()[0]


def test_admin_controls_section(admin_env):
    conn, client, _ = admin_env
    html = client.get("/admin").text
    assert 'id="controls"' in html and "Reconnect scanner" in html and "Restart worker" in html
    r = client.post("/admin/commands", data={"target": "capture", "action": "reconnect"},
                    headers={"HX-Request": "true"})
    assert "waiting to be picked up" in r.text and 'hx-trigger="every 3s"' in r.text
    assert client.post("/admin/commands", data={"target": "x", "action": "y"},
                       headers={"HX-Request": "true"}).status_code == 400
    # reboot: confirmation, the automatic-reboot threshold, and the request is queued
    assert "Reboot scanner" in html and "hx-confirm" in html and "3 min" in html
    r = client.post("/admin/commands", data={"target": "capture", "action": "reboot"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200 and "capture reboot" in r.text


def test_reboot_command(tmp_path, conn):
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path))
    commands.post(conn, "capture", "reboot")
    conn.commit()
    src = LiveLike()
    asyncio.run(handle_commands(cfg, src, None))
    assert getattr(src, "rebooted", False) and src.asked == 0
    assert "rebooting the scanner" in conn.execute("SELECT result FROM commands").fetchone()[0]
