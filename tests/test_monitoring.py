import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from scanner_transcriber import db, statestore
from scanner_transcriber.app import run_pipeline
from scanner_transcriber.config import Config, StorageConfig
from scanner_transcriber.monitoring import CaptureReporter, HealthMonitor
from scanner_transcriber.source.replay import ReplaySource

from segsim import Sim
from test_replay_app import write_session


def events(conn):
    return [(e["kind"], e["level"]) for e in reversed(statestore.recent_events(conn))]


def test_reporter_outage_sequence(conn):
    r = CaptureReporter(conn)
    retry = datetime.now(UTC) + timedelta(seconds=4)
    r.on_source_state("connecting")
    r.on_source_state("streaming")
    r.on_source_state("reconnecting", error="no audio for 5.1s", next_retry_at=retry)
    r.on_source_state("connecting")
    r.on_source_state("reconnecting", error="OSError: host unreachable", next_retry_at=retry)
    row = statestore.states(conn)["capture"]
    assert row["state"] == "reconnecting"
    assert row["detail"]["disconnected_at"] and "unreachable" in row["detail"]["last_error"]
    r.on_source_state("connecting")
    r.on_source_state("streaming")
    assert events(conn) == [("connected", "info"), ("disconnected", "warn"),
                            ("reconnected", "info")]   # one event per outage, not per retry
    row = statestore.states(conn)["capture"]
    assert row["state"] == "streaming" and "disconnected_at" not in row["detail"]
    assert "last_error" not in row["detail"]


def test_reporter_scanner_off_at_startup(conn):
    r = CaptureReporter(conn)
    r.on_source_state("connecting")
    r.on_source_state("reconnecting", error="timeout", next_retry_at=datetime.now(UTC))
    [(kind, level)] = events(conn)
    assert (kind, level) == ("disconnected", "warn")
    assert "Can't reach" in statestore.recent_events(conn)[0]["message"]


def test_reporter_session_busy(conn):
    r = CaptureReporter(conn)
    r.on_source_state("session_busy", error="another process holds it",
                      next_retry_at=datetime.now(UTC))
    r.on_source_state("session_busy", error="another process holds it",
                      next_retry_at=datetime.now(UTC))
    assert events(conn) == [("session_busy", "error")]


def test_monitor_logs_transitions_once(conn, tmp_path, monkeypatch):
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path))
    fake_disk = {"disk_free_gb": 3.0, "disk_free_pct": 40.0}
    monkeypatch.setattr("scanner_transcriber.health.host_info", lambda d: dict(fake_disk))
    m = HealthMonitor(conn, cfg)
    m.check()
    m.check()                                  # unchanged: no duplicate event
    fake_disk["disk_free_gb"] = 1.0
    m.check()                                  # warn → error
    fake_disk["disk_free_gb"] = 50.0
    m.check()                                  # cleared
    assert events(conn) == [("disk_low", "warn"), ("disk_low", "error"),
                            ("disk_low_cleared", "info")]


def test_replay_run_publishes_capture_state(tmp_path):
    sim = Sim(6).status(0.2, "Mute").ptt(1.0, 3.0, "1101", unit="UID:1")
    write_session(tmp_path / "session", sim)
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path / "data"))
    asyncio.run(run_pipeline(cfg, ReplaySource(tmp_path / "session")))
    conn = db.connect(cfg.storage.db_path)
    cap = statestore.states(conn)["capture"]
    kinds = [k for k, _ in events(conn)]
    conn.close()
    assert cap["state"] == "stopped" and cap["detail"]["source"] == "replay"
    assert cap["detail"]["last_audio_at"] and cap["detail"]["last_status_at"]
    assert kinds == ["started", "stopped"]


def test_reporter_scanner_reboot_events(conn):
    r = CaptureReporter(conn)
    r.on_source_state("streaming")
    r.on_source_state("reconnecting", error="ConnectionRefusedError: refused")
    r.on_source_state("rebooting", error="audio unavailable for 3 min", auto=True, acknowledged=True)
    row = statestore.states(conn)["capture"]
    assert row["state"] == "rebooting" and row["detail"]["reboots"] == 1
    r.on_source_state("streaming")
    assert events(conn) == [("connected", "info"), ("disconnected", "warn"),
                            ("scanner_reboot", "warn"), ("reconnected", "info")]
    r.on_source_state("rebooting", error="requested from the Admin page", auto=False,
                      acknowledged=False)
    r.on_source_state("needs_power_cycle", error="audio still refused after 3 automatic reboots")
    msgs = [e["message"] for e in statestore.recent_events(conn, limit=2)]
    assert "power-cycle the scanner" in msgs[0]
    assert "Admin" in msgs[1] and "no acknowledgement" in msgs[1]
