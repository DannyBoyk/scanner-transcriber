"""Admin in a real browser (scratch instance with a known password): login, prompt counter,
save + revert, logout. Saves a screenshot of the Admin page if a path is given."""
import sys
import time

from common import BASE, browser, check, connect, finish, wait
from selenium.webdriver.common.by import By

DB, PASSWORD = sys.argv[1], sys.argv[2]
SHOT = sys.argv[3] if len(sys.argv) > 3 else None
conn = connect(DB)
d = browser(1280, 1800)
try:
    d.get(BASE + "/admin")
    check("logged out → login page", "/login" in d.current_url)
    pw = d.find_element(By.NAME, "password"); pw.send_keys("wrong"); pw.submit()
    check("wrong password rejected", wait(lambda: "Wrong password" in d.page_source))
    time.sleep(1.2)
    pw = d.find_element(By.NAME, "password"); pw.send_keys(PASSWORD); pw.submit()
    check("login lands on Admin", wait(lambda: d.current_url.endswith("/admin") and "Log out" in d.page_source))
    ta = d.find_element(By.CSS_SELECTOR, "#prompt textarea")
    before = d.find_element(By.ID, "token-count").text
    ta.send_keys(" Tanker 2, Engine 3.")
    check("token counter updates while typing", wait(lambda: d.find_element(By.ID, "token-count").text != before))
    d.find_element(By.CSS_SELECTOR, "#prompt input[name=note]").send_keys("browser test")
    d.find_element(By.CSS_SELECTOR, "#prompt button[type=submit]").click()
    check("save → new active version", wait(lambda: "applies from the next transmission" in d.page_source))
    check("…stored in the DB", "Tanker 2" in conn.execute(
        "SELECT v.body FROM active_versions a JOIN prompt_versions v ON v.id=a.version_id WHERE a.kind='prompt'").fetchone()[0])
    if SHOT:
        d.save_screenshot(SHOT)
    d.find_element(By.CSS_SELECTOR, "#prompt details.history summary").click()
    d.find_element(By.XPATH, "//section[@id='prompt']//button[normalize-space()='Make active']").click()
    check("revert makes the old version active", wait(lambda: "is active again" in d.page_source))
    # clean start: preview counts, refuses a wrong confirmation (never actually deletes here)
    d.find_element(By.XPATH, "//section[@id='clean']//button[contains(., 'Check what it would delete')]").click()
    check("clean start shows what it would delete", wait(lambda: "would remove" in d.find_element(By.ID, "clean").text))
    d.find_element(By.CSS_SELECTOR, "#clean input[name=confirm]").send_keys("delete")
    d.find_element(By.CSS_SELECTOR, "#clean button[type=submit]").click()
    check("…and refuses without DELETE in capitals", wait(lambda: "type DELETE" in d.find_element(By.ID, "clean").text))
    check("…queuing nothing", conn.execute("SELECT COUNT(*) FROM commands WHERE action='clean-start'").fetchone()[0] == 0)
    d.find_element(By.XPATH, "//form[@action='/logout']/button").click()
    d.get(BASE + "/admin")
    check("logout → login required again", "/login" in d.current_url)
finally:
    d.quit()
finish()
