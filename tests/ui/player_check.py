"""Continuous playback in a real browser (scratch instance, short real clips):
order, skips, pauses, speed, listen-along, behind-live + jump, jump-to auto-load, keys."""
import functools
import sys
import time
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path

import common
import numpy as np
from common import BASE, browser, check, connect, finish, insert_tx
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

DB, DATA = sys.argv[1], Path(sys.argv[2])
conn = connect(DB)
common.WAIT_S = 10
wait = functools.partial(common.wait, step=0.1)       # playback timing checks poll faster
tone = (3000 * np.sin(np.arange(2400) * 0.2)).astype(np.int16)        # 0.3 s clip


def write_wav(path, pcm):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000); w.writeframes(pcm.astype("<i2").tobytes())


def add(start, tgid="1101", nospeech=0, text="clear", channel="Dispatch"):
    tx = insert_tx(conn, start, 0.3, tgid=tgid, channel=channel, no_speech=nospeech,
                   transcript="" if nospeech else text)
    rel = f"clips/p{tx}.wav"
    (DATA / "clips").mkdir(parents=True, exist_ok=True)
    write_wav(DATA / rel, tone)
    conn.execute("UPDATE transmissions SET audio_path = ? WHERE id = ?", (rel, tx))
    return tx


base = datetime.now(UTC) - timedelta(minutes=10)
ids = [add(base + timedelta(seconds=0), "1101", text="one"),
       add(base + timedelta(seconds=5), "1101", text="two"),
       add(base + timedelta(seconds=8), "1101", nospeech=1),
       add(base + timedelta(seconds=12), "1201", text="three", channel="Ashford")]
d = browser(1280, 1000, autoplay=True)
js = d.execute_script
REC = """window.__plays=[]; window.__errs=[]; window.addEventListener('error', e => __errs.push(String(e.message)));
const a=document.getElementById('player'); a.addEventListener('play', () => __plays.push([a.src.split('/').slice(-2)[0], performance.now()]));"""
try:
    d.get(BASE + "/"); time.sleep(0.8); js(REC)
    d.find_element(By.CSS_SELECTOR, "[data-play-all]").click()
    check("player bar appears", wait(lambda: js("return !document.getElementById('player-bar').hidden")))
    check("live mode is labelled: '● Live' with what happens next",
          wait(lambda: (d.find_element(By.CSS_SELECTOR, ".pb-mode").is_displayed()
               and d.find_element(By.CSS_SELECTOR, ".pb-mode-name").text == "Live"
               and "then live" in d.find_element(By.CSS_SELECTOR, ".pb-mode-info").text)
               or "newest" in d.find_element(By.CSS_SELECTOR, ".pb-mode-info").text))
    check("…the stop button says 'Stop listening'", "Stop listening" in d.find_element(By.CSS_SELECTOR, "[data-pb=stop]").text)
    check("…first time: the hint explains it keeps going",
          d.find_element(By.CSS_SELECTOR, ".pb-hint").is_displayed() and js("return localStorage.getItem('liveHintSeen')") == "1")
    check("'play from here' is visible on every row without hovering (desktop)",
          float(js("return getComputedStyle(document.querySelector('button.play-from')).opacity")) >= 0.7)
    check("the live page's button says what it does", "Catch up & listen live" in d.find_element(By.CSS_SELECTOR, "[data-play-all]").text)
    check("row buttons say they keep up with live",
          "keep up with live" in d.find_element(By.CSS_SELECTOR, "button.play-from").get_attribute("title"))
    check("plays all shown, oldest first, skipping no-speech",
          wait(lambda: [int(p[0]) for p in js("return __plays")] == [ids[0], ids[1], ids[3]]))
    plays = js("return __plays")
    gap_same = plays[1][1] - plays[0][1]; gap_tg = plays[2][1] - plays[1][1]
    check(f"talkgroup change pauses longer ({gap_tg:.0f} ms vs {gap_same:.0f} ms)", gap_tg > gap_same + 600)
    check("listen-along: waits at the end", wait(lambda: js("return Player._state().waiting")))
    check("…and says so: 'Caught up. New transmissions play as they come in.' with a pulsing dot",
          wait(lambda: "Caught up" in d.find_element(By.CSS_SELECTOR, ".pb-mode-info").text
               and "is-waiting" in d.find_element(By.CSS_SELECTOR, ".pb-mode").get_attribute("class")))
    new = add(datetime.now(UTC), "1201", text="four", channel="Ashford")
    check("…and plays a newly arrived transmission", wait(lambda: int(js("return __plays")[-1][0]) == new, t=12))
    # picking one transmission while listen-along is waiting: single mode, no "waiting" text
    d.find_element(By.CSS_SELECTOR, f"#tx-{ids[0]} button.play").click()
    check("single ▶ while waiting: plays that clip", wait(lambda: int(js("return __plays")[-1][0]) == ids[0]))
    check("…and the bar no longer says 'waiting'",
          wait(lambda: "waiting" not in d.find_element(By.CSS_SELECTOR, ".pb-text").text and
               js("return Player._state().mode") == "single" and not js("return Player._state().waiting")))
    check("…single clip: no mode line, plain stop button",
          not d.find_element(By.CSS_SELECTOR, ".pb-mode").is_displayed()
          and "Stop listening" not in d.find_element(By.CSS_SELECTOR, "[data-pb=stop]").text)
    check("…and 'Jump to live' / 'Follow' aren't shown for a single clip",
          not d.find_element(By.CSS_SELECTOR, "[data-pb=live]").is_displayed() and
          not d.find_element(By.CSS_SELECTOR, "[data-pb=follow]").is_displayed())
    js("document.querySelector('[data-pb=speed]').value='2'; document.querySelector('[data-pb=speed]').dispatchEvent(new Event('change'))")
    check("speed applied", js("return document.getElementById('player').playbackRate") == 2)
    # volume: above 100% routes through Web Audio (gain + limiter), created on a real click
    js("const v=document.querySelector('[data-pb=volume]'); v.value='150'; v.dispatchEvent(new Event('input'))")
    n_before = js("return __plays.length")
    d.find_element(By.CSS_SELECTOR, f"#tx-{ids[1]} button.play").click()
    check("volume 150% → Web Audio gain 1.5, element at full volume",
          wait(lambda: js("return Player._state().gain") == 1.5 and js("return Player._state().element_volume") == 1))
    check("…and it still plays", wait(lambda: js("return __plays.length") > n_before))
    check("…shown as 150%", "150%" in d.find_element(By.CSS_SELECTOR, ".pb-vol-val").text)
    js("const v=document.querySelector('[data-pb=volume]'); v.value='60'; v.dispatchEvent(new Event('input'))")
    check("volume 60% → gain 0.6", wait(lambda: abs(js("return Player._state().gain") - 0.6) < 1e-6))
    body = d.find_element(By.TAG_NAME, "body")
    body.send_keys(Keys.ESCAPE)
    check("Esc stops and hides the bar", wait(lambda: js("return document.getElementById('player-bar').hidden")))

    d.get(BASE + "/"); time.sleep(0.8)
    check("volume remembered after reload", js("return Player._state().vol") == 60 and
          d.find_element(By.CSS_SELECTOR, "[data-pb=volume]").get_attribute("value") == "60")
    js("localStorage.removeItem('playerVolume')")

    # behind live: start from the oldest while newer rows exist
    old = [add(datetime.now(UTC) - timedelta(minutes=30) + timedelta(seconds=i * 3), "1101", text=f"old {i}") for i in range(3)]
    d.get(BASE + "/"); time.sleep(0.8); js(REC)
    for _ in range(5):                                    # page_size is small: load older pages
        if d.find_elements(By.CSS_SELECTOR, f"#tx-{old[0]}"):
            break
        d.find_element(By.CSS_SELECTOR, "li.more-older button").click(); time.sleep(0.6)
    d.find_element(By.CSS_SELECTOR, f"#tx-{old[0]} button.play-from").click()
    check("'play from here' starts at that row", wait(lambda: js("return __plays.length") > 0 and int(js("return __plays")[0][0]) == old[0]))
    js("document.getElementById('player').pause()")
    check("shows how far behind live", wait(lambda: "behind live" in d.find_element(By.CSS_SELECTOR, ".pb-mode-info").text
                                          and d.find_element(By.CSS_SELECTOR, ".pb-mode-info").is_displayed()))
    check("the hint only shows the first time", not d.find_element(By.CSS_SELECTOR, ".pb-hint").is_displayed())
    check("'Jump to live' is shown while behind", d.find_element(By.CSS_SELECTOR, "[data-pb=live]").is_displayed())
    check("'Follow' isn't shown until you scroll away", not d.find_element(By.CSS_SELECTOR, "[data-pb=follow]").is_displayed())
    d.find_element(By.CSS_SELECTOR, "[data-pb=live]").click()
    check("Jump to live plays the newest", wait(lambda: int(js("return __plays")[-1][0]) == new))
    body = d.find_element(By.TAG_NAME, "body")            # re-find after navigation
    body.send_keys(" ")
    check("Space pauses", wait(lambda: js("return document.getElementById('player').paused")))
    body.send_keys(Keys.ESCAPE)

    # jump-to view with more newer rows than shown → auto-loads "Newer"
    at = (datetime.now(UTC) - timedelta(minutes=30)).astimezone().strftime("%Y-%m-%dT%H:%M")
    d.get(BASE + f"/?at={at}"); time.sleep(0.8); js(REC)
    d.find_element(By.CSS_SELECTOR, "[data-play-all]").click()
    check("jump-to: keeps going past the loaded page (auto-loads newer)",
          wait(lambda: new in [int(p[0]) for p in js("return __plays")], t=20))
    body = d.find_element(By.TAG_NAME, "body"); body.send_keys(Keys.ESCAPE)

    # search results: a finite queue, labelled as such
    d.get(BASE + "/search?q=old"); time.sleep(0.8); js(REC)
    d.find_element(By.CSS_SELECTOR, "[data-play-all]").click()
    check("search: 'Results · 1 of N · stops at the end', no live dot, plain stop",
          wait(lambda: d.find_element(By.CSS_SELECTOR, ".pb-mode-name").text == "Results"
               and "stops at the end" in d.find_element(By.CSS_SELECTOR, ".pb-mode-info").text
               and not d.find_element(By.CSS_SELECTOR, ".pb-dot").is_displayed()
               and "Stop listening" not in d.find_element(By.CSS_SELECTOR, "[data-pb=stop]").text))
    check("search: row buttons say they play to the end of the results",
          "end of these results" in d.find_element(By.CSS_SELECTOR, "button.play-from").get_attribute("title"))
    # Listen live owns the lock-screen controls while it plays; the clip player takes them back
    d.get(BASE + "/listen"); time.sleep(0.8); js(REC)
    check("listen page: nothing claims the lock-screen controls at load",
          js("return window.__mediaOwner || null") is None)
    d.find_element(By.ID, "listen-btn").click()
    check("listen page: starting the stream claims them, titled 'Scanner – live'",
          wait(lambda: js("return window.__mediaOwner") == "stream"
               and js("return navigator.mediaSession.metadata && navigator.mediaSession.metadata.title") == "Scanner – live"))
    check("listen page: the stream actually plays", wait(lambda: js("return !document.getElementById('listen-audio').paused")))
    d.find_element(By.ID, "listen-btn").click()
    d.get(BASE + "/"); time.sleep(0.8); js(REC)
    d.find_element(By.CSS_SELECTOR, "li.tx button.play").click()
    check("live page: playing a clip hands the lock-screen controls to the clip player",
          wait(lambda: js("return window.__mediaOwner") == "player"))
    d.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)

    # portrait phone: volume is a 🔊 button that opens a large slider above the bar
    d.set_window_size(390, 844)
    d.get(BASE + "/"); time.sleep(0.8); js(REC)
    d.find_element(By.CSS_SELECTOR, "li.tx button.play").click()
    slider = lambda: d.find_element(By.CSS_SELECTOR, "[data-pb=volume]")
    check("phone: the slider is tucked away behind 🔊", wait(lambda: not slider().is_displayed()))
    d.find_element(By.CSS_SELECTOR, "[data-pb=vol-toggle]").click()
    check("phone: tapping 🔊 opens a slider wide enough to use",
          wait(lambda: slider().is_displayed() and slider().size["width"] >= 200))
    js("document.body.click()")                         # a tap anywhere outside the bar
    check("phone: tapping elsewhere closes it", wait(lambda: not slider().is_displayed()))
    d.set_window_size(1280, 1000)
    check("no JS errors", js("return window.__errs.length") == 0)
finally:
    d.quit()
finish()
