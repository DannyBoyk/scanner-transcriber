"""Edit talkgroup settings in a real browser (scratch web instance): alias, colour, toggles.
Editing needs the admin login (M4.5): pass the scratch password as the second argument."""
import sys
from datetime import UTC, datetime

from common import BASE, browser, check, connect, finish, insert_tx, now, wait
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

DB, PASSWORD = sys.argv[1], sys.argv[2]
conn = connect(DB)
conn.execute("""INSERT INTO channels (key, system, department, channel, tgid, first_heard_at, last_heard_at, color)
                VALUES ('sys:Sys|tg:1518', 'Sys', 'Unknown', 'TGID:1518', '1518', ?, ?, 0)""", (now(), now()))
insert_tx(conn, datetime.now(UTC), channel_key="sys:Sys|tg:1518", tgid="1518", channel="TGID:1518",
          department="Unknown", transcript="mystery talkgroup")
d = browser()
try:
    d.get(BASE + "/channels")
    check("logged out: no edit boxes", not d.find_elements(By.CSS_SELECTOR, "li.ch input[name=alias]"))
    d.get(BASE + "/login?next=/channels")
    pw = d.find_element(By.NAME, "password"); pw.send_keys(PASSWORD); pw.submit()
    wait(lambda: "/login" not in d.current_url and d.find_elements(By.CSS_SELECTOR, "li.ch input[name=alias]"))
    box = d.find_element(By.CSS_SELECTOR, "li.ch input[name=alias]")
    box.send_keys("Mystery TG", Keys.TAB)                        # change event → PATCH
    check("alias saved from the page", wait(lambda: conn.execute("SELECT alias FROM channels").fetchone()[0] == "Mystery TG"))
    check("row redraws with 'Saved'", wait(lambda: "Saved" in d.find_element(By.CSS_SELECTOR, "li.ch").text))
    d.find_elements(By.CSS_SELECTOR, "li.ch .swatch-btn")[3].click()
    check("colour saved", wait(lambda: conn.execute("SELECT color FROM channels").fetchone()[0] == 3))
    btn = next(b for b in d.find_elements(By.CSS_SELECTOR, "li.ch .toggle-btn") if "Live feed" in b.text)
    btn.click()
    check("hidden toggled", wait(lambda: conn.execute("SELECT hidden FROM channels").fetchone()[0] == 1))
    check("button now says hidden", wait(lambda: "Live feed: hidden" in d.find_element(By.CSS_SELECTOR, "li.ch").text))
    d.get(BASE + "/")
    check("hidden talkgroup not on the live feed", "mystery talkgroup" not in d.page_source)
    check("…but its chip is there (dimmed)", "hidden-tg" in d.page_source and "Mystery TG" in d.page_source)
finally:
    d.quit()
finish()
