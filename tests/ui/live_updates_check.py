"""Drive headless Firefox against a scratch web instance and exercise live updates."""
import json
import sys
import time
from datetime import UTC, datetime, timedelta

from common import BASE, browser, check, connect, finish, insert_tx, now, wait

DB = sys.argv[1]
conn = connect(DB)


def add(tgid, start_off, dur=2.0, status="queued", transcript=None, channel="Dispatch", dept="County Sheriff"):
    return insert_tx(conn, datetime.now(UTC) + timedelta(seconds=start_off), dur, tgid=tgid,
                     status=status, transcript=transcript, channel=channel, department=dept)


d = browser()
js = d.execute_script
try:
    add("1101", -120, status="done", transcript="older call")      # something already on the page
    d.get(BASE + "/")
    js("window.__errs=[]; window.addEventListener('error', e => window.__errs.push(String(e.message)));")
    heads0 = js("return document.querySelectorAll('li.xhead').length")
    a = add("1101", -5)                                               # new, same TG but > 10 s after 'older call'
    check("new row appears live", wait(lambda: js(f"return !!document.getElementById('tx-{a}')")))
    check("…with its own exchange header", js("return document.querySelectorAll('li.xhead').length") == heads0 + 1)
    check("…shows 'transcribing…'", "transcribing" in js(f"return document.getElementById('tx-{a}').textContent"))
    conn.execute("UPDATE transmissions SET status='done', transcript='Medic 3 en route', updated_at=? WHERE id=?", (now(), a))
    check("transcript swaps in place", wait(lambda: "Medic 3 en route" in js(f"return document.getElementById('tx-{a}').textContent")))
    b = add("1101", -2, dur=1.0)                                      # 3 s after a ended → same exchange
    check("reply joins the top exchange", wait(lambda: js(f"return !!document.getElementById('tx-{b}')")))
    check("…no extra header", js("return document.querySelectorAll('li.xhead').length") == heads0 + 1)
    check("…placed newest-first under the header",
          js(f"return document.getElementById('tx-{b}').previousElementSibling.classList.contains('xhead') && document.getElementById('tx-{b}').nextElementSibling.id === 'tx-{a}'"))
    c = add("1201", 0, channel="Dispatch", dept="Town Police")   # other talkgroup
    check("other talkgroup starts a new block", wait(lambda: js("return document.querySelectorAll('li.xhead').length") == heads0 + 2))
    check("a newly heard talkgroup gets its filter chip without a reload",
          wait(lambda: js("return !!document.querySelector('#tg-chips .chip-tg[data-tg=\"1201\"]')"), t=4))   # instant, not the 10 s batch
    check("…and the chip counts are fresh (TG 1101: 3 in 24 h)",
          wait(lambda: js("return [...document.querySelectorAll('#tg-chips .chip-tg')].find(a => a.title.includes('TG 1101')).querySelector('.n').textContent") == "3", t=15))
    # scrolled down → buffered behind the pill
    js("document.body.style.minHeight='4000px'; window.scrollTo(0, 1500)")
    time.sleep(0.5)
    e1 = add("1002", 1, channel="11B Response", dept="County Fire/Rescue")
    check("while reading further down, new rows wait behind the pill",
          wait(lambda: js("return !document.getElementById('new-pill').hidden")) and not js(f"return !!document.getElementById('tx-{e1}')"))
    check("pill says 1 new", "1 new" in js("return document.getElementById('new-pill').textContent"))
    js("document.getElementById('new-pill').click()")
    check("clicking the pill shows it", wait(lambda: js(f"return !!document.getElementById('tx-{e1}')")))
    js("window.scrollTo(0,0)")
    js("document.getElementById('pause').click()")
    f1 = add("1002", 3, channel="11B Response", dept="County Fire/Rescue")
    check("paused: row buffered", wait(lambda: js("return !document.getElementById('new-pill').hidden")) and not js(f"return !!document.getElementById('tx-{f1}')"))
    js("document.getElementById('pause').click()")
    check("resume: buffered row appears", wait(lambda: js(f"return !!document.getElementById('tx-{f1}')")))
    # status dot goes green when components report healthy
    check("dot starts red (nothing running)", "level-error" in js("return document.getElementById('status-dot-slot').innerHTML"))
    for name, state in (("capture", "streaming"), ("worker", "gpu")):
        conn.execute("INSERT OR REPLACE INTO component_state (name, state, detail, pid, updated_at) VALUES (?,?,?,?,?)",
                     (name, state, json.dumps({"source": "live"}), 1, now()))
    check("dot leaves red live when components report in (≤ ~6 s)", wait(lambda: "level-error" not in js("return document.getElementById('status-dot-slot').innerHTML"), t=9))
    # dropped connection: close the stream from the client side and make sure it resumes without gaps
    js("window.__es_test = true")
    g = add("1101", 6)
    check("still live after all that", wait(lambda: js(f"return !!document.getElementById('tx-{g}')")))
    check("no duplicate rows", js("const ids=[...document.querySelectorAll('li.tx')].map(e=>e.id); return ids.length===new Set(ids).size"))
    check("no JS errors", js("return window.__errs.length") == 0)
finally:
    d.quit()

# theme button: Auto follows the device; an explicit Light/Dark overrides it and is remembered
LIGHT_BG, DARK_BG = "rgb(246, 247, 249)", "rgb(17, 20, 24)"
for dark_device in (False, True):
    d = browser(dark_device=dark_device)
    js = d.execute_script
    dev = "dark" if dark_device else "light"
    bg = lambda: js("return getComputedStyle(document.body).backgroundColor")
    theme = lambda: js("return document.documentElement.dataset.theme || 'auto'")
    try:
        d.get(BASE + "/")
        check(f"{dev} device, Auto: follows the device", theme() == "auto" and bg() == (DARK_BG if dark_device else LIGHT_BG))
        want = ["light", "dark"]
        btn = lambda: d.find_element("css selector", "#theme-toggle")
        btn().click()
        check(f"{dev} device: Light overrides it", theme() == "light" and wait(lambda: bg() == LIGHT_BG, t=2))
        btn().click()
        check(f"{dev} device: Dark overrides it", theme() == "dark" and wait(lambda: bg() == DARK_BG, t=2))
        d.get(BASE + "/status")
        check(f"{dev} device: the choice is remembered on the next page, before first paint",
              js("return document.documentElement.dataset.theme") == "dark" and bg() == DARK_BG
              and "Dark" in btn().get_attribute("title"))
        btn().click()
        check(f"{dev} device: and back to Auto", theme() == "auto" and "Auto" in btn().get_attribute("title"))
    finally:
        d.quit()
finish()
