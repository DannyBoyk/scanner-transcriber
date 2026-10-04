// Scanner Transcriber: continuous playback ("play everything").
//
// One shared <audio id="player">. Two modes:
//   * single:  a row's ▶ plays just that clip
//   * queue:   "⏵⏵ Play from here" / "Play all" play forward in time (oldest → newest),
//              back-to-back with short pauses; on the live feed it then keeps playing new
//              transmissions as they arrive (listen-along)
// The queue is built from the rows' data attributes, not tied to the elements, so the live
// row cap or the "N new" pill never disturb playback. No-speech clips are skipped.
(function () {
  "use strict";
  const GAP_MS = 500;           // between clips
  const TG_GAP_MS = 1500;       // when the talkgroup changes
  const BEHIND_S = 30;          // show "behind live" beyond this
  const FOLLOW_PAUSE_MS = 8000; // after the user scrolls, stop auto-following for a while
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* ignore */ } },
  };

  const audio = document.getElementById("player");
  const bar = document.getElementById("player-bar");
  if (!audio || !bar) return;
  const $ = (sel) => bar.querySelector(sel);
  const feedEl = document.getElementById("feed");
  const liveMode = !!feedEl && feedEl.dataset.mode === "live";

  // ---- state -------------------------------------------------------------------------------
  let items = [];        // queue, chronological
  let index = -1;
  let mode = "idle";     // idle | single | queue
  let waiting = false;   // queue exhausted on the live feed: waiting for new transmissions
  let gapTimer = null;
  let followUntil = 0;   // auto-follow suspended until this time (ms)
  let singleBtn = null;
  let rate = parseFloat(store.get("playbackRate") || "1") || 1;
  // volume 0-200%. A plain <audio> can't go above 100% (and iOS ignores .volume entirely), so
  // away from 100% the audio is routed through Web Audio: gain → limiter. That graph is only
  // created during a user gesture (browsers keep an AudioContext suspended otherwise).
  let vol = Math.min(200, Math.max(0, parseInt(store.get("playerVolume") || "100", 10) || 100));
  let actx = null, gainNode = null;

  function itemFromRow(li) {
    const d = li.dataset;
    return {
      id: li.id.replace("tx-", ""), src: d.src || null, tg: d.tg || "", label: d.label || "",
      hue: d.hue || "215", time: d.time || "", text: d.text || "", started: Date.parse(d.started),
      nospeech: d.nospeech === "1",
    };
  }
  const rowEl = (it) => document.getElementById(`tx-${it.id}`);
  const playable = (it) => !!it && !!it.src && !it.nospeech;

  function collect() {
    const seen = new Set();
    const out = [];
    document.querySelectorAll("li.tx").forEach((li) => {
      if (seen.has(li.id)) return;
      seen.add(li.id);
      out.push(itemFromRow(li));
    });
    out.sort((a, b) => a.started - b.started || Number(a.id) - Number(b.id));
    return out;
  }

  // ---- playback ----------------------------------------------------------------------------
  function ensureGraph() {
    if (actx) { if (actx.state !== "running") actx.resume().catch(() => {}); return; }
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    try {
      actx = new AC();
      const src = actx.createMediaElementSource(audio);
      gainNode = actx.createGain();
      const limiter = actx.createDynamicsCompressor();   // keeps boosted peaks from clipping
      limiter.threshold.value = -2; limiter.knee.value = 0; limiter.ratio.value = 20;
      limiter.attack.value = 0.003; limiter.release.value = 0.15;
      src.connect(gainNode).connect(limiter).connect(actx.destination);
    } catch (e) { actx = null; gainNode = null; }
  }

  function applyVolume() {
    if (gainNode) { audio.volume = 1; gainNode.gain.value = vol / 100; }
    else audio.volume = Math.min(1, vol / 100);
    const slider = $("[data-pb=volume]");
    if (slider) slider.value = String(vol);
    const label = $(".pb-vol-val");
    if (label) label.textContent = `${vol}%`;
  }

  function gesture() {           // call from user-initiated events only
    if (vol !== 100 || actx) ensureGraph();
    applyVolume();
  }

  function applyRate() {
    audio.playbackRate = rate;
    audio.preservesPitch = true;
    audio.mozPreservesPitch = true;
    audio.webkitPreservesPitch = true;
  }

  function highlight(it) {
    document.querySelectorAll("li.tx.now-playing").forEach((n) => n.classList.remove("now-playing"));
    const li = it && rowEl(it);
    if (!li) return;
    li.classList.add("now-playing");
    if (Date.now() >= followUntil) li.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  function setButtons(it, on) {
    document.querySelectorAll("button.play.playing").forEach((b) => {
      b.classList.remove("playing"); b.firstElementChild.textContent = "▶";
    });
    const li = it && rowEl(it);
    const b = li && li.querySelector("button.play");
    if (b && on) { b.classList.add("playing"); b.firstElementChild.textContent = "❚❚"; }
  }

  function render() {
    bar.hidden = mode === "idle";
    document.body.classList.toggle("has-player", mode !== "idle");
    const it = items[index];
    $("[data-pb=toggle]").textContent = audio.paused && !waiting ? "▶" : "⏸";
    $("[data-pb=speed]").value = String(rate);
    const now = $("[data-pb=now]");
    if (it) {
      now.style.setProperty("--h", it.hue);
      now.href = `/t/${it.id}`;
      now.querySelector(".pb-label").textContent = it.label;
      now.querySelector(".pb-time").textContent = it.time;
      now.querySelector(".pb-text").textContent = it.text ? ` ${it.text}` : "";
    }
    const lag = it && liveMode && mode === "queue" && !waiting ? (Date.now() - it.started) / 1000 : 0;
    $("[data-pb=live]").hidden = !(lag > BEHIND_S && hasLater());
    renderMode(lag);
    $("[data-pb=follow]").hidden = !(mode === "queue" && Date.now() < followUntil);
    $("[data-pb=prev]").disabled = mode !== "queue";
    $("[data-pb=next]").disabled = mode !== "queue";
    updateMediaSession(it);
  }

  // Mode line at the left of the bar: says whether this ends ("Results · 4 of 12") or keeps
  // going ("● Live"), in the same words as the button that started it.
  function renderMode(lag) {
    const box = $(".pb-mode");
    const queue = mode === "queue";
    box.hidden = !queue;
    const stopText = $(".pb-stop-text");
    const live = queue && liveMode;
    stopText.textContent = live ? " Stop listening" : "";
    const stop = $("[data-pb=stop]");
    stop.classList.toggle("pb-stop-wide", live);
    stop.setAttribute("aria-label", live ? "Stop listening (Esc)" : "Stop (Esc)");
    stop.title = stop.getAttribute("aria-label");
    if (!queue) return;
    box.classList.toggle("is-live", live);
    box.classList.toggle("is-waiting", live && waiting);
    $(".pb-mode-name").textContent = live ? "Live" : "Results";
    const rest = items.slice(index + 1).filter(playable).length;
    let info;
    if (live && waiting) info = "Caught up. New transmissions play as they come in.";
    else if (live) {
      const parts = [];
      if (lag > BEHIND_S) parts.push(`${fmtLag(lag)} behind live`);
      parts.push(rest ? `${rest} to go, then live` : "playing the newest");
      info = parts.join(" · ");
    } else {
      const all = items.filter(playable);
      const pos = all.indexOf(items[index]) + 1;
      info = `${Math.max(pos, 1)} of ${all.length} · stops at the end`;
    }
    $(".pb-mode-info").textContent = info;
  }

  // One-time explanation the first time live listening is started in this browser.
  let hintTimer = null;
  function maybeHint() {
    if (!liveMode || store.get("liveHintSeen") === "1") return;
    store.set("liveHintSeen", "1");
    const hint = $(".pb-hint");
    hint.hidden = false;
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => { hint.hidden = true; }, 6000);
  }

  // Row buttons say what they'll do on this page (rows arrive later too: label them as they do).
  const PLAY_FROM_LABEL = liveMode ? "Play from here, then keep up with live"
    : feedEl ? "Play from here onwards" : "Play from here to the end of these results";
  function labelRows(root) {
    (root || document).querySelectorAll("button.play-from").forEach((b) => {
      b.title = PLAY_FROM_LABEL; b.setAttribute("aria-label", PLAY_FROM_LABEL);
    });
  }

  function fmtLag(s) {
    s = Math.round(s);
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
    return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  }

  const hasLater = () => items.slice(index + 1).some(playable);

  function playItem(i) {
    clearTimeout(gapTimer);
    claimMediaSession();
    index = i;
    waiting = false;
    const it = items[i];
    audio.src = it.src;
    applyRate();
    highlight(it);
    audio.play().then(() => { setButtons(it, true); render(); })
      .catch(() => { setButtons(it, false); render(); });
    render();
  }

  function advance(step = 1) {
    let i = index + step;
    while (i >= 0 && i < items.length && !playable(items[i])) i += step;
    if (i >= 0 && i < items.length) return playItem(i);
    if (step > 0) return exhausted();
  }

  function exhausted() {
    // jump-to view: pull in the next newer page and carry on
    const newer = document.querySelector("li.more-newer button");
    if (newer && !liveMode) {
      waiting = true;
      render();
      newer.click();
      return;
    }
    if (liveMode) { waiting = true; setButtons(null, false); render(); return; }
    stop();
  }

  function stop() {
    clearTimeout(gapTimer);
    audio.pause();
    audio.removeAttribute("src");
    mode = "idle"; items = []; index = -1; waiting = false;
    $(".pb-hint").hidden = true;
    highlight(null); setButtons(null, false); render();
  }

  audio.addEventListener("ended", () => {
    if (mode === "single") { setButtons(null, false); mode = "idle"; highlight(null); render(); return; }
    if (mode !== "queue") return;
    const cur = items[index];
    let j = index + 1;
    while (j < items.length && !playable(items[j])) j++;
    const nxt = items[j];
    const gap = nxt && cur && nxt.tg !== cur.tg ? TG_GAP_MS : GAP_MS;
    setButtons(null, false);
    gapTimer = setTimeout(() => advance(1), gap / Math.max(rate, 1));
  });
  ["pause", "play"].forEach((e) => audio.addEventListener(e, () => {
    const it = items[index];
    if (it) setButtons(it, e === "play");
    render();
  }));

  // ---- public actions ----------------------------------------------------------------------
  function playFrom(li) {
    items = collect();
    const id = li ? li.id.replace("tx-", "") : null;
    let i = id ? items.findIndex((it) => it.id === id) : 0;
    if (i < 0) i = 0;
    mode = "queue";
    followUntil = 0;
    maybeHint();
    while (i < items.length && !playable(items[i])) i++;
    if (i < items.length) playItem(i); else exhausted();
  }

  function playOne(btn) {
    const li = btn.closest("li.tx");
    if (mode === "single" && singleBtn === btn && !audio.paused) { audio.pause(); return; }
    clearTimeout(gapTimer);
    mode = "single"; singleBtn = btn;
    claimMediaSession();
    waiting = false;            // leaving listen-along: no longer waiting for new transmissions
    items = [li ? itemFromRow(li) : { id: "", src: btn.dataset.src }]; index = 0;
    const it = items[0];
    if (!it.src) it.src = btn.dataset.src;
    audio.src = it.src; applyRate();
    highlight(li ? it : null);
    audio.play().then(() => setButtons(it, true)).catch(() => setButtons(it, false));
    render();
  }

  function toggle() {
    if (mode === "idle") return playFrom(null);
    if (waiting) return;
    if (audio.paused) audio.play(); else audio.pause();
  }

  function jumpToLive() {
    let i = items.length - 1;
    while (i > index && !playable(items[i])) i--;
    if (i > index) playItem(i);
  }

  // Called by app.js for every SSE `tx` event (new row or update).
  function onTx(d, isNew) {
    const li = document.createElement("template");
    li.innerHTML = d.item.trim();
    const fresh = itemFromRow(li.content.firstElementChild);
    const k = items.findIndex((it) => it.id === fresh.id);
    if (k >= 0) {                      // e.g. transcript arrived / turned out to be no speech
      items[k] = fresh;
      if (k === index) render();
      return;
    }
    if (!isNew || mode !== "queue" || !liveMode) return;
    if (items.length && fresh.started < items[items.length - 1].started) return;
    items.push(fresh);
    if (waiting && playable(fresh)) advance(1);
    else render();
  }

  // htmx loaded more rows (jump-to "Newer"): add them to the queue and continue
  document.body.addEventListener("htmx:afterSwap", () => {
    if (mode !== "queue") return;
    const known = new Set(items.map((it) => it.id));
    const cur = items[index];
    const added = collect().filter((it) => !known.has(it.id) && (!cur || it.started >= cur.started));
    if (!added.length) { if (waiting && !liveMode) stop(); return; }
    items = items.concat(added).sort((a, b) => a.started - b.started || Number(a.id) - Number(b.id));
    index = cur ? items.findIndex((it) => it.id === cur.id) : -1;
    if (waiting) advance(1);
  });

  // ---- wiring ------------------------------------------------------------------------------
  // portrait phones: the volume slider opens above the bar; any tap elsewhere closes it
  function setVolOpen(open) {
    bar.classList.toggle("vol-open", open);
    $("[data-pb=vol-toggle]").setAttribute("aria-expanded", String(open));
  }
  document.addEventListener("click", (ev) => {
    if (bar.classList.contains("vol-open") && !ev.target.closest(".pb-vol")) setVolOpen(false);
  }, true);

  document.addEventListener("click", (ev) => {
    if (ev.target.closest("button.play, button.play-from, [data-play-all], [data-pb]")) gesture();
    const one = ev.target.closest("button.play");
    if (one) { playOne(one); return; }
    const from = ev.target.closest("button.play-from");
    if (from) { playFrom(from.closest("li.tx")); return; }
    if (ev.target.closest("[data-play-all]")) { playFrom(null); return; }
    const pb = ev.target.closest("[data-pb]");
    if (!pb || pb.tagName === "SELECT" || pb.tagName === "A" || pb.tagName === "INPUT") return;
    const act = pb.dataset.pb;
    if (act === "toggle") toggle();
    else if (act === "prev" && mode === "queue") advance(-1);
    else if (act === "next" && mode === "queue") advance(1);
    else if (act === "stop") stop();
    else if (act === "live") jumpToLive();
    else if (act === "follow") { followUntil = 0; highlight(items[index]); render(); }
    else if (act === "vol-toggle") setVolOpen(!bar.classList.contains("vol-open"));
  });
  $("[data-pb=volume]").addEventListener("input", (e) => {
    vol = Math.min(200, Math.max(0, parseInt(e.target.value, 10) || 0));
    store.set("playerVolume", String(vol));
    gesture();
  });
  applyVolume();
  labelRows();
  document.body.addEventListener("htmx:afterSwap", (e) => labelRows(e.target));
  if (feedEl) new MutationObserver(() => labelRows(feedEl)).observe(feedEl, { childList: true });
  $("[data-pb=speed]").addEventListener("change", (e) => {
    rate = parseFloat(e.target.value) || 1;
    store.set("playbackRate", String(rate));
    applyRate(); render();
  });
  // the user scrolling suspends auto-follow for a while
  ["wheel", "touchmove"].forEach((e) => window.addEventListener(e, () => {
    if (mode === "queue") { followUntil = Date.now() + FOLLOW_PAUSE_MS; render(); }
  }, { passive: true }));
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (mode !== "idle") gesture();
    if (e.key === " " && mode !== "idle") { e.preventDefault(); toggle(); }
    else if (e.key === "ArrowRight" && mode === "queue") { e.preventDefault(); advance(1); }
    else if (e.key === "ArrowLeft" && mode === "queue") { e.preventDefault(); advance(-1); }
    else if (e.key === "Escape" && bar.classList.contains("vol-open")) setVolOpen(false);
    else if (e.key === "Escape" && mode !== "idle") stop();
  });
  setInterval(() => { if (mode === "queue") render(); }, 5000);   // keep "behind live" fresh

  // Lock-screen / car controls. Claimed only when this player starts playing (not at page
  // load): the Listen live page has its own audio and its own handlers, and registering ours
  // at load used to override them, so lock-screen pause paused the idle clip player instead.
  function updateMediaSession(it) {
    if (!("mediaSession" in navigator) || mode === "idle" || window.__mediaOwner !== "player") return;
    try {
      navigator.mediaSession.metadata = it && window.MediaMetadata
        ? new MediaMetadata({ title: it.text || "(transcribing…)", artist: it.label, album: it.time })
        : null;
    } catch (e) { /* ignore */ }
  }
  function claimMediaSession() {
    if (!("mediaSession" in navigator)) return;
    window.__mediaOwner = "player";
    const ms = navigator.mediaSession;
    const set = (a, f) => { try { ms.setActionHandler(a, f); } catch (e) { /* unsupported */ } };
    set("play", () => toggle());
    set("pause", () => audio.pause());
    set("nexttrack", () => mode === "queue" && advance(1));
    set("previoustrack", () => mode === "queue" && advance(-1));
    set("stop", () => stop());
  }

  window.Player = { onTx, playFrom, stop, _state: () => ({ mode, index, waiting, rate, vol, gain: gainNode ? gainNode.gain.value : null,
    element_volume: audio.volume, ids: items.map((i) => i.id) }) };
})();
