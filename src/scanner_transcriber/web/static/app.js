// Scanner Transcriber: page behaviour.
//  - live updates over Server-Sent Events (/events): new rows, transcript updates, status dot
//  - pause / "N new" pill so the list doesn't move while you read
//  - play buttons (one shared <audio>), hide-no-speech toggle, jump-to marker scroll
(function () {
  "use strict";
  const GROUP_GAP_S = 10;  // must match web/feed.py
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode etc. */ } },
  };
  const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };

  // ---- theme: Auto (follow the device) → Light → Dark, remembered per browser ---------------
  const THEMES = {
    auto: { label: "Auto (follows your device)",
      svg: '<circle cx="8" cy="8" r="6.25" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 1.75a6.25 6.25 0 0 0 0 12.5z" fill="currentColor"/>' },
    light: { label: "Light",
      svg: '<circle cx="8" cy="8" r="3" fill="currentColor"/><g stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M8 1v1.6M8 13.4V15M1 8h1.6M13.4 8H15M3.05 3.05l1.13 1.13M11.82 11.82l1.13 1.13M3.05 12.95l1.13-1.13M11.82 4.18l1.13-1.13"/></g>' },
    dark: { label: "Dark",
      svg: '<path d="M13.6 10.1A6 6 0 0 1 5.9 2.4a6 6 0 1 0 7.7 7.7z" fill="currentColor"/>' },
  };
  const ORDER = ["auto", "light", "dark"];
  const themeBtn = document.getElementById("theme-toggle");
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
    else { t = "auto"; delete document.documentElement.dataset.theme; }
    store.set("theme", t);
    if (themeBtn) {
      themeBtn.innerHTML = `<svg viewBox="0 0 16 16" aria-hidden="true">${THEMES[t].svg}</svg>`;
      themeBtn.title = `Theme: ${THEMES[t].label}`;
      themeBtn.setAttribute("aria-label", `Theme: ${THEMES[t].label}. Click to change.`);
      themeBtn.dataset.choice = t;
    }
  }
  applyTheme(store.get("theme"));
  if (themeBtn) themeBtn.addEventListener("click", () =>
    applyTheme(ORDER[(ORDER.indexOf(themeBtn.dataset.choice || "auto") + 1) % ORDER.length]));

  // ---- hide no-speech (remembered per browser) ----------------------------------------------
  const hide = document.getElementById("hide-nospeech");
  if (hide) {
    const apply = () => document.body.classList.toggle("hide-nospeech", hide.checked);
    hide.checked = store.get("hideNoSpeech") === "1";
    apply();
    hide.addEventListener("change", () => { store.set("hideNoSpeech", hide.checked ? "1" : "0"); apply(); });
  }

  // Play buttons, "play from here" and the player bar live in static/player.js.

  // ---- jump-to: bring the time marker into view -----------------------------------------------
  const mark = document.getElementById("anchor");
  if (mark) mark.scrollIntoView({ block: "center" });

  // ---- live feed ----------------------------------------------------------------------------
  const feed = document.getElementById("feed");
  const live = !!feed && feed.dataset.mode === "live";
  const pill = document.getElementById("new-pill");
  const pauseBtn = document.getElementById("pause");
  let paused = false;
  const buffer = [];

  const readingDown = () => window.scrollY > 300;

  function updatePill() {
    if (!pill) return;
    pill.hidden = buffer.length === 0;
    pill.textContent = `${buffer.length} new — show`;
  }

  function topRow() { return feed.querySelector("li.tx"); }

  function insertNew(d) {
    const top = topRow();
    if (top && d.started_at < top.dataset.started) return;  // older than what's shown: leave it
    feed.querySelectorAll("li.more.end").forEach((n) => n.remove());  // "Nothing recorded yet"
    const item = el(d.item);
    const sameDay = top && top.dataset.day === d.day;
    const joins = top && sameDay && d.tgid && top.dataset.tg === d.tgid &&
      (Date.parse(d.started_at) - Date.parse(top.dataset.ended)) / 1000 < GROUP_GAP_S;
    if (joins) {
      const head = top.previousElementSibling;  // the exchange header
      head.after(item);                         // newest first, just under the header
      return;
    }
    const first = feed.firstElementChild;
    const anchor = (sameDay && first && first.classList.contains("day-break")) ? first.nextSibling : first;
    const frag = document.createDocumentFragment();
    if (!sameDay) frag.append(el(`<li class="day-break" aria-hidden="true"></li>`));
    if (!sameDay) frag.firstChild.textContent = d.day;
    frag.append(el(d.head), item);
    feed.insertBefore(frag, anchor);
  }

  function flush() {
    while (buffer.length) insertNew(buffer.shift());
    updatePill();
    trim();
  }

  // Keep a long-open live page from growing forever: beyond max rows, drop the oldest from the
  // bottom (with any header / day divider left empty) and end the list with "Load older" at the
  // new oldest row. Only while at the top of the live view: never under someone reading.
  const MAX_ROWS = parseInt((feed && feed.dataset.maxRows) || "300", 10);
  function trim() {
    if (!live || paused || readingDown()) return;
    let rows = feed.querySelectorAll("li.tx");
    if (rows.length <= MAX_ROWS) return;
    const keepLast = rows[MAX_ROWS - 1];
    while (feed.lastElementChild && feed.lastElementChild !== keepLast) feed.lastElementChild.remove();
    const tg = feed.dataset.tg || "";
    const more = el(`<li class="more more-older"><button type="button" class="btn"
        hx-get="/fragments/feed?before=${encodeURIComponent(keepLast.dataset.cursor)}&tg=${encodeURIComponent(tg)}"
        hx-target="closest li" hx-swap="outerHTML">Load older</button></li>`);
    feed.append(more);
    if (window.htmx) window.htmx.process(more);
  }

  // ---- talkgroup chips: new talkgroups appear and counts stay fresh without a reload ---------
  const CHIPS_EVERY_MS = 10000;
  let chipsTimer = null, chipsLast = 0;
  function refreshChips() {
    chipsTimer = null; chipsLast = Date.now();
    const tg = (feed && feed.dataset.tg) || "";
    fetch(`/fragments/tg-chips?tg=${encodeURIComponent(tg)}`)
      .then((r) => (r.ok ? r.text() : null))
      .then((html) => {
        const cur = document.getElementById("tg-chips");
        if (html && cur) cur.replaceWith(el(html));
      })
      .catch(() => { /* next transmission tries again */ });
  }
  function scheduleChips() {
    if (chipsTimer) return;
    chipsTimer = setTimeout(refreshChips, Math.max(0, CHIPS_EVERY_MS - (Date.now() - chipsLast)));
  }
  // A talkgroup with no chip yet: refresh right away (counts alone wait for the 10 s batch).
  const askedFor = new Set();   // each new talkgroup triggers at most one immediate refresh
  function chipsFor(tgid) {
    const known = !tgid || askedFor.has(tgid) ||
      document.querySelector(`#tg-chips .chip-tg[data-tg="${CSS.escape(tgid)}"]`);
    if (known) return scheduleChips();
    askedFor.add(tgid);
    clearTimeout(chipsTimer);
    refreshChips();
  }

  function onTx(d) {
    const existing = document.getElementById(`tx-${d.id}`);
    if (live && !existing) chipsFor(d.tgid);
    if (window.Player) window.Player.onTx(d, !existing);   // queue sees new rows even if buffered
    if (existing) {                        // transcript arrived / status changed: swap in place
      const fresh = el(d.item);
      if (existing.classList.contains("now-playing")) fresh.classList.add("now-playing");
      const btn = existing.querySelector("button.play.playing");
      existing.replaceWith(fresh);
      if (btn) { const b = fresh.querySelector("button.play"); b.classList.add("playing"); b.firstElementChild.textContent = "❚❚"; }
      return;
    }
    const queued = buffer.findIndex((b) => b.id === d.id);
    if (queued >= 0) { buffer[queued] = d; return; }
    if (!live) return;
    if (paused || readingDown()) { buffer.push(d); updatePill(); return; }
    insertNew(d);
    trim();
  }

  if (pill) pill.addEventListener("click", () => { flush(); window.scrollTo({ top: 0, behavior: "smooth" }); });
  if (pauseBtn) pauseBtn.addEventListener("click", () => {
    paused = !paused;
    pauseBtn.setAttribute("aria-pressed", String(paused));
    pauseBtn.textContent = paused ? "Resume" : "Pause";
    if (!paused) flush();
  });
  window.addEventListener("scroll", () => { if (!paused && !readingDown() && buffer.length) flush(); }, { passive: true });

  // ---- SSE connection (status dot on every page; rows on the live feed) -------------------------
  const slot = document.getElementById("status-dot-slot");
  let es = null;
  let lastId = null;

  function connect() {
    if (!window.EventSource) return;
    const p = new URLSearchParams({ since: document.body.dataset.since || "" });
    if (live) { p.set("feed", "1"); if (feed.dataset.tg) p.set("tg", feed.dataset.tg); }
    if (lastId) p.set("last", lastId);    // a fresh EventSource doesn't send Last-Event-ID
    es = new EventSource(`/events?${p}`);
    es.addEventListener("tx", (e) => { lastId = e.lastEventId || lastId; onTx(JSON.parse(e.data)); });
    es.addEventListener("status", (e) => { if (slot) slot.innerHTML = JSON.parse(e.data).html; });
    es.onerror = () => {
      // The browser retries on its own (with Last-Event-ID) unless the stream was closed for good.
      if (es.readyState === EventSource.CLOSED) setTimeout(connect, 3000);
    };
  }
  // Phones may kill the stream while asleep: reconnect on wake, resuming from the last event.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && (!es || es.readyState === EventSource.CLOSED)) connect();
  });
  connect();
})();
