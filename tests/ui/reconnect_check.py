"""Reconnect after a web restart (scratch instance): the page loses its live connection while
the server is down; rows written meanwhile must all arrive afterwards, without duplicates.
This check starts and stops the scratch web server itself."""
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

from common import BASE, browser, check, connect, finish, insert_tx, wait

DB, CFG, REPO = sys.argv[1], sys.argv[2], sys.argv[3]


def start():
    return subprocess.Popen([f"{REPO}/.venv/bin/python", "-m", "scanner_transcriber", "--config", CFG, "web"],
                            cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def add(tgid, off):
    conn = connect(DB)
    try:
        return insert_tx(conn, datetime.now(UTC) + timedelta(seconds=off), tgid=tgid, transcript=f"row {off}")
    finally:
        conn.close()


srv = start(); time.sleep(3)
d = browser()
js = d.execute_script
shown = lambda tx: js(f"return !!document.getElementById('tx-{tx}')")
try:
    d.get(BASE + "/"); time.sleep(1)
    a = add("1101", 0)
    check("before the outage: a new row appears live", wait(lambda: shown(a)))
    srv.terminate(); srv.wait(); print("server stopped")
    missed = [add("1101", 30 + i * 20) for i in range(3)]      # written while the page has no connection
    time.sleep(2); srv = start(); print("server restarted")
    check("rows written during the outage all arrive after reconnect",
          wait(lambda: all(shown(m) for m in missed), t=15, step=0.3))
    check("no duplicates",
          js("const i=[...document.querySelectorAll('li.tx')].map(e=>e.id); return i.length===new Set(i).size"))
finally:
    d.quit(); srv.terminate()
finish()
