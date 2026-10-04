"""Long-session check: the live feed trims itself to [web] live_max_rows (run the scratch web
instance with a small cap, e.g. live_max_rows = 8, page_size = 5)."""
import sys
import time
from datetime import UTC, datetime, timedelta

import common
from common import BASE, browser, check, connect, finish, insert_tx, wait

DB, CAP = sys.argv[1], int(sys.argv[2])
conn = connect(DB)
common.WAIT_S = 8


def add(off, tgid="1101"):
    return insert_tx(conn, datetime.now(UTC) + timedelta(seconds=off), tgid=tgid, transcript=f"row {off}")


d = browser()
js = d.execute_script
try:
    for i in range(3):
        add(-600 + i * 30, tgid=("1101", "1201")[i % 2])
    d.get(BASE + "/"); time.sleep(1)
    n = lambda: js("return document.querySelectorAll('li.tx').length")
    ids = [add(-300 + i * 20, tgid=("1101", "1201", "1002")[i % 3]) for i in range(CAP + 4)]
    check(f"page stays at the cap ({CAP}) after {len(ids) + 3} rows",
          wait(lambda: js(f"return !!document.getElementById('tx-{ids[-1]}')")) and n() == CAP)
    check("newest rows kept, oldest dropped",
          js(f"return !!document.getElementById('tx-{ids[-1]}')") and not js(f"return !!document.getElementById('tx-{ids[0]}')"))
    check("list ends with 'Load older'",
          js("const l=document.getElementById('feed').lastElementChild; return l.classList.contains('more-older')"))
    check("no empty exchange header or day divider left at the bottom",
          js("const l=document.getElementById('feed').lastElementChild.previousElementSibling; return l.classList.contains('tx')"))
    js("document.querySelector('li.more-older button').click()")
    check("'Load older' brings the trimmed rows back", wait(lambda: n() > CAP) and
          js(f"return !!document.getElementById('tx-{ids[0]}')"))
    js("document.body.style.minHeight='5000px'; window.scrollTo(0, 2000)"); time.sleep(0.4)
    before = n(); extra = add(10)
    time.sleep(2)
    check("no trimming while reading further down (row waits behind the pill)",
          n() == before and not js(f"return !!document.getElementById('tx-{extra}')"))
finally:
    d.quit()
finish()
