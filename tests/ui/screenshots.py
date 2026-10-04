"""Takes the README screenshots (docs/screenshots/) from a scratch web instance filled by
demo_data.py. Run by screenshots.sh, which starts and stops the instance.

    screenshots.py DB_PATH OUT_DIR
"""
from __future__ import annotations

import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

from selenium.webdriver.common.by import By

import common
from common import BASE, browser, connect, wait

DB, OUT = sys.argv[1], sys.argv[2]
PASSWORD = "demo-pass-123"


def heartbeat(stop: threading.Event) -> None:
    """Pretend capture and the worker are running, so the status dot is green."""
    conn = connect(DB)
    start = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    while not stop.is_set():
        now = datetime.now(UTC).isoformat()
        cap = {"source": "live", "started_at": start, "reconnects": 0, "rtp_packets_lost": 0,
               "connected_since": start, "last_status_at": now, "current": None, "last_audio_at": now}
        wrk = {"model": "OpenVINO/whisper-small.en-int8-ov", "mode": "gpu", "degraded_reason": None,
               "processed": 4180, "current": None}
        for name, state, detail in [("capture", "streaming", cap), ("worker", "gpu", wrk)]:
            conn.execute("INSERT OR REPLACE INTO component_state (name, state, detail, pid, updated_at) "
                         "VALUES (?,?,?,?,?)", (name, state, json.dumps(detail), 1, common.now()))
        stop.wait(2)


def shot(d, name: str, settle: float = 1.0) -> None:
    time.sleep(settle)
    d.save_screenshot(f"{OUT}/{name}.png")
    print("saved", name, flush=True)


def desktop(theme: str = "light"):
    d = browser(1280, 820, autoplay=True, dark_device=theme == "dark", scale=2)
    return d


def main() -> None:
    stop = threading.Event()
    threading.Thread(target=heartbeat, args=(stop,), daemon=True).start()
    time.sleep(1)
    try:
        for theme in ("light", "dark"):
            d = desktop(theme)
            d.get(BASE + "/")
            wait(lambda: d.find_elements(By.CSS_SELECTOR, ".tx"))
            shot(d, f"live-{theme}")
            d.quit()

        d = desktop()
        # playing: start from an earlier row and let it follow on
        d.get(BASE + "/")
        wait(lambda: d.find_elements(By.CSS_SELECTOR, ".tx"))
        btns = d.find_elements(By.CSS_SELECTOR, ".tx .play-from")
        if btns:
            d.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click()", btns[6])
        shot(d, "playing", 2.5)

        d.get(BASE + "/search?q=Main+Street")
        shot(d, "search")
        [(tx,)] = connect(DB).execute(
            "SELECT id FROM transmissions WHERE transcript LIKE 'Medic 7 transporting%'").fetchall()
        d.get(BASE + f"/t/{tx}")   # the Medic 7 hospital report
        shot(d, "detail")
        d.get(BASE + "/status")
        shot(d, "status")
        d.get(BASE + "/admin")
        pw = d.find_element(By.NAME, "password")
        pw.send_keys(PASSWORD)
        pw.submit()
        wait(lambda: d.current_url.endswith("/admin") and "Log out" in d.page_source, 15)
        shot(d, "admin")
        d.get(BASE + "/channels")
        shot(d, "talkgroups")
        d.quit()

        # phone: live feed, and Listen live playing a new transmission
        d = browser(390, 844, autoplay=True, scale=2)
        d.get(BASE + "/")
        shot(d, "phone-live", 2)
        d.get(BASE + "/listen")
        d.find_element(By.ID, "listen-btn").click()
        time.sleep(2)
        conn = connect(DB)
        path, tg, dep, ch, key = conn.execute(
            "SELECT audio_path, tgid, department, channel, channel_key FROM transmissions "
            "WHERE duration_ms > 6000 ORDER BY id DESC LIMIT 1").fetchone()
        common.insert_tx(conn, datetime.now(UTC) - timedelta(seconds=8), 7, audio_path=path, tgid=tg,
                         department=dep, channel=ch, channel_key=key, unit_id="UID:4051",
                         transcript="168, show me back in service.")
        # the page shows its own address as the stream URL; show a typical tailnet one instead
        d.execute_script("document.querySelector('.listen-notes code').textContent ="
                         " 'https://scanner.your-tailnet.ts.net/stream.mp3'")
        shot(d, "phone-listen", 12)   # listener count refreshes every 10 s
        d.quit()
    finally:
        stop.set()


if __name__ == "__main__":
    main()
