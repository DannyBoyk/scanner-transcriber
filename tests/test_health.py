from datetime import UTC, datetime, timedelta

import pytest

from scanner_transcriber.config import StatusConfig
from scanner_transcriber.health import assess, overall

NOW = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
CFG = StatusConfig()


def ago(s):
    return (NOW - timedelta(seconds=s)).isoformat()


def comp(state, age=1, **detail):
    return {"state": state, "detail": detail, "pid": 1, "updated_at": ago(age)}


HEALTHY = {"capture": comp("streaming"), "worker": comp("gpu")}
QUEUE_OK = {"queued": 0, "oldest_queued_age_s": None, "failed_recent": 0}
HOST_OK = {"disk_free_gb": 200.0, "disk_free_pct": 80.0}


def kinds(components=HEALTHY, queue=QUEUE_OK, host=HOST_OK):
    conds = assess(components, queue, host, CFG, NOW)
    return {(c.kind, c.level) for c in conds}, overall(conds), conds


def test_all_healthy():
    k, level, _ = kinds()
    assert k == set() and level == "ok"


@pytest.mark.parametrize("capture,expected", [
    (None, ("capture_never_ran", "error")),
    (comp("stopped"), ("capture_stopped", "error")),
    (comp("streaming", age=60), ("capture_down", "error")),
    (comp("reconnecting", disconnected_at=ago(20)), ("reconnecting", "warn")),
    (comp("reconnecting", disconnected_at=ago(90)), ("reconnecting", "error")),
    (comp("connecting", disconnected_at=ago(90)), ("reconnecting", "error")),
    (comp("session_busy", last_error="RTSP SETUP 453"), ("session_busy", "error")),
])
def test_capture_conditions(capture, expected):
    comps = {**HEALTHY, "capture": capture} if capture else {"worker": comp("gpu")}
    k, level, _ = kinds(comps)
    assert k == {expected} and level == expected[1]


def test_startup_connecting_is_not_an_outage():
    k, _, _ = kinds({**HEALTHY, "capture": comp("connecting")})
    assert k == set()


def test_reconnect_message_includes_retry_and_error():
    cap = comp("reconnecting", disconnected_at=ago(80), last_error="no audio for 5.2s",
               next_retry_at=(NOW + timedelta(seconds=8)).isoformat())
    _, _, [c] = kinds({**HEALTHY, "capture": cap})
    assert "1m 20s" in c.message and "next try in 8s" in c.message and "no audio" in c.message


@pytest.mark.parametrize("worker,expected", [
    (None, ("worker_down", "error")),
    (comp("stopped"), ("worker_down", "error")),
    (comp("gpu", age=120), ("worker_down", "error")),
    (comp("degraded", degraded_reason="GPU device not found"), ("degraded", "warn")),
])
def test_worker_conditions(worker, expected):
    comps = {**HEALTHY, "worker": worker} if worker else {"capture": comp("streaming")}
    k, _, _ = kinds(comps)
    assert k == {expected}


def test_failed_clips_warn():
    k, level, _ = kinds(queue={**QUEUE_OK, "failed_recent": 2})
    assert k == {("failed_clips", "warn")} and level == "warn"


@pytest.mark.parametrize("wait,expected", [(60, None), (180, "warn"), (900, "error")])
def test_backlog(wait, expected):
    k, _, _ = kinds(queue={"queued": 5, "oldest_queued_age_s": wait, "failed_recent": 0})
    assert k == ({("backlog", expected)} if expected else set())


@pytest.mark.parametrize("free_gb,pct,expected", [
    (200, 80, None), (4, 50, "warn"), (20, 8, "warn"), (1.5, 1, "error")])
def test_disk(free_gb, pct, expected):
    k, _, _ = kinds(host={"disk_free_gb": free_gb, "disk_free_pct": pct})
    assert k == ({("disk_low", expected)} if expected else set())


def test_overall_takes_worst():
    comps = {"capture": comp("reconnecting", disconnected_at=ago(10)),
             "worker": comp("degraded")}
    _, level, _ = kinds(comps, host={"disk_free_gb": 1, "disk_free_pct": 1})
    assert level == "error"


def test_scanner_rebooting_is_amber_and_needs_power_cycle_is_red():
    k, level, conds = kinds({"capture": comp("rebooting", disconnected_at=ago(200)),
                             "worker": comp("gpu")})
    assert k == {("rebooting", "warn")} and "Rebooting the scanner" in conds[0].message
    k, level, conds = kinds({"capture": comp("needs_power_cycle",
                                             last_error="audio still refused after 3 automatic reboots"),
                             "worker": comp("gpu")})
    assert k == {("needs_power_cycle", "error")} and level == "error"
    assert "power-cycle the scanner" in conds[0].message and "3 automatic" in conds[0].message
