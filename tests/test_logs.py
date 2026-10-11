"""Rotating file logs and the Admin log viewer."""
import logging

from scanner_transcriber import logsetup


def test_setup_writes_file_and_tail_filters(tmp_path):
    logsetup.setup("worker", tmp_path, console=False)
    log = logging.getLogger("scanner_transcriber.test")
    log.info("clip 1 done")
    log.warning("GPU slow")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("clip 2 failed")
    for h in logging.getLogger().handlers:
        h.flush()
    lines = logsetup.tail(tmp_path, "worker")
    assert [ln["level"] for ln in lines] == ["INFO", "WARNING", "ERROR"]
    assert "Traceback" in lines[-1]["text"] and "ValueError: boom" in lines[-1]["text"]  # kept together
    warn = logsetup.tail(tmp_path, "worker", min_level="WARNING")
    assert [ln["level"] for ln in warn] == ["WARNING", "ERROR"]
    assert logsetup.tail(tmp_path, "web") == []                         # no file yet
    logging.getLogger().handlers.clear()


def test_admin_logs_section(admin_env):
    conn, client, d = admin_env
    p = logsetup.log_path(d, "capture")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("2026-09-30 12:00:00,000 INFO x: scanner connected\n"
                 "2026-09-30 12:00:05,000 WARNING x: RTP gap\n")
    html = client.get("/admin").text
    assert 'id="logs"' in html and "scanner connected" in html and "lv-warning" in html
    frag = client.get("/admin/fragments/logs?c=capture&level=WARNING").text
    assert "RTP gap" in frag and "scanner connected" not in frag
    assert 'hx-trigger="every 5s"' in client.get("/admin/fragments/logs?c=capture&auto=1").text
    client.post("/logout")
    assert client.get("/admin/fragments/logs").status_code == 401
