# M3 plan: web UI (live feed, detail, audio, system status)

**Status:** planned in detail (agreed with Dan 2026-09-30). **Steps 1–5 done
2026-09-30** (status plumbing; web skeleton with the status dot and `/status`; live feed
with jump-to; live updates over SSE; detail page and audio); see the step notes at the end.
Step 6: Dan reports phone/desktop testing "looking good" (2026-09-30). **Still pending: the
scanner Ethernet unplug test** (Dan away from home; do it on the next visit). See "Pending
tests" below. Search and channel editing are M4.

**Goal:** a web page, usable on a phone and a desktop, that shows transmissions as they happen,
fills in each transcript when it's ready, plays the audio, and always shows whether the logger
is working.

## Exit criteria

- **Jump to:** picking a past date and time (native picker on phone and desktop) shows the
  transmissions around that moment, with newer/older paging and a way back to live.
- **Live feed:** a new transmission appears within about 1 s of being saved, and its transcript
  replaces "transcribing…" when ready, without reloading. After a network blip or phone sleep
  the feed resumes with **no gaps and no duplicates**.
- **Audio** plays and seeks in desktop Chrome/Firefox and **iPhone Safari** (Safari needs HTTP
  Range support).
- **Detail page** shows everything known about a transmission.
- **System status:** during a scanner unplug test the header dot goes amber → red → green and
  the event log records the outage. `/healthz` returns 503 while capture is stopped.
- **The web keeps working while capture is stopped**, and restarting the web never touches the
  scanner's audio session.
- Tests pass: page rendering, the change feed (resume, no gaps, no duplicates), Range requests,
  health conditions.

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Devices | **Phone and desktop**: responsive layout, one column on a phone, wider rows on a desktop. | Dan |
| Flagged / no-speech rows | **Shown dimmed** with a small flag label, plus a **"hide no-speech" toggle** (remembered per browser). | Dan: nothing silently disappears |
| Listen-along auto-play | **Later** (not M3). Per-clip play buttons only. | Dan |
| Access during M3 | **Home LAN**: served on the NUC's LAN address, no login, read-only. Moves to `tailscale serve` + localhost in M5. | Dan |
| Process | A **separate `web` process** (own command; its own systemd unit in M5). Not inside `run`. | It keeps reporting status while capture is down, and restarting it never touches the scanner session |
| Stack | FastAPI + Uvicorn, Jinja2 server-rendered pages, **htmx** + its SSE extension, hand-written CSS (light/dark from the OS). htmx files are **vendored** into `static/`, with no CDN at runtime. | Design §3; no build step; works offline |
| Live updates | **SSE** (`/events`) fed by **polling the database** every 0.5 s. | Design §5.7: simple and robust with two writer processes |
| Times | Shown in **local time** (`[web] timezone`, default the system's), e.g. `15:04:12`. Stored as UTC. | Matches the clip filenames |
| DB access | Read-only per request (short-lived connections, WAL). The web writes nothing in M3. | Channel edits come in M4 |

## Pages

### Header (every page)

App name, nav (**Live**, **Status**), and the **status dot** (see System status). On a phone
the nav collapses to icons.

### `/` Live feed

- **Rows, newest at the top:** local time · talkgroup label (M4 alias if set, else channel name,
  else `TG 1101`) · department (small) · unit ID (small) · duration · **play button** ·
  transcript. "transcribing…" is shown until the transcript arrives.
- **Exchanges grouped visually:** consecutive rows on the same talkgroup less than 10 s apart
  share a thin left rule and don't repeat the talkgroup label. That's the one-row-per-key-up
  decision from M1.
- **Flag styling:** `no speech` rows are dimmed. Hallucination, prompt-leak and degraded rows
  get a small chip with an explanatory tooltip.
- **Controls:**
  - **Talkgroup filter chips** (talkgroups heard in the last 24 h). They're in the URL
    (`?tg=1101,1201`), so they're bookmarkable.
  - **Hide no-speech** toggle.
  - **Pause:** new rows collect behind a "12 new — show" pill instead of pushing the list
    while you read.
- **Paging:** the latest 50, with **"load older"** at the bottom (keyset by `started_at, id`).
- **Jump to a date and time** (added 2026-09-30 at Dan's request):
  - A **"Jump to…"** control uses the browser's native picker (`<input type="datetime-local">`).
    That's a calendar and time wheel on a phone, a calendar dropdown on a desktop, with no
    extra library.
  - **Shortcuts:** "1 hour ago", "This morning" (06:00), "Last night" (21:00 yesterday),
    "Yesterday same time".
  - Picking a time opens the feed **anchored there**: live updates are paused, with about 25
    transmissions before and after the moment and a marker line at the chosen time, plus
    **"newer"** and **"older"** buttons. A **"Back to live"** button returns to the live view.
  - The time is in the URL (`/?at=2026-09-29T21:00`, local time) so it can be bookmarked or
    shared. It combines with the talkgroup filter (`/?at=…&tg=1201`).
  - The picker's range is limited to the oldest stored transmission and now. If audio for the
    period has been purged by retention, a note says so; transcripts remain.
- **Live updates:** new rows are prepended, and transcript updates swap the existing row in
  place (htmx out-of-band swap by row id).

### `/t/{id}` Transmission detail

- Audio player (seekable) and a **download** link (`2026-09-30_150412_TG1101_1234.wav`).
- The transcript, large, with flags explained in plain words. Raw model output and Parakeet's
  text are shown when they differ.
- **Metadata:**
  - when (local and UTC), duration, system / department / channel / TGID;
  - unit IDs, site, frequency, RSSI;
  - whether the clip is a continuation or was cut while status was missing.
- **Processing:** model, degraded or not, processing time, attempts, errors.
- **Context:** the 5 transmissions before and after on the same talkgroup (the conversation),
  with prev/next links.
- **Collapsible raw data:** status changes during the clip, and the scanner's raw status XML.

### `/status` System status

See below.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/transmissions` | List: `tg` (repeatable), `before` / `after` (keyset cursors), `at` (anchor time: rows around it), `limit`, `hide_no_speech`. Full search filters come in M4. |
| GET | `/api/transmissions/{id}` | Detail JSON |
| GET | `/api/transmissions/{id}/audio` | WAV with **HTTP Range** support; `410 Gone` if retention removed it |
| GET | `/api/status` | Health JSON (the evaluator below) |
| GET | `/events` | SSE: `transmission.created`, `transmission.updated`, `status.changed` |
| GET | `/healthz` | 200 when ok/warn, 503 when error |

### Change feed (SSE) details

- **Polling:** the server polls `transmissions` by `updated_at` every 0.5 s and pushes
  **rendered HTML rows** (htmx swaps them straight in), plus a small JSON payload for other
  clients.
- **No missed rows:** two processes write the table, so a row can commit slightly after a
  newer timestamp has been seen. Each poll therefore re-reads a 2-second overlap window and
  drops rows already sent (by `id` + `updated_at`).
- **Resume:** each event carries `id: <updated_at>|<id>`. A reconnecting browser sends
  `Last-Event-ID` and gets everything since, so there are no gaps after phone sleep.
- **Filtering:** the SSE connection carries the page's talkgroup filter and the server filters.
  A 15-second keep-alive comment stops proxies from closing it.
- **`status.changed`** is sent when the overall health level or any message changes. It swaps
  the header dot.

### Audio

- `FileResponse` with Range support (checked with Safari, which requires it for
  `<audio>`); `preload="none"`, so a page of 50 rows doesn't download 50 clips.
- Clips are 16-bit WAV, about 16 KB/s, so no transcoding is needed.

## System status

**Goal:** you can always tell whether the logger is working. A problem shows as a coloured
indicator on every page, and a `/status` page explains it and keeps a history of outages.

### What Dan asked for

- **Where:** a small **indicator in the page header** (green / amber / red dot, with a tooltip
  summarising any problems) on every page, linking to a full **`/status` page**. No banner.
- **Conditions that raise a warning:**
  1. **Scanner unreachable:** can't connect, no audio or status for several seconds,
     reconnecting, or another program holds the scanner's single audio session.
  2. **Transcription problems:** worker down, **degraded mode** (the iGPU failed, so Parakeet
     is running on the CPU), or clips failing.
  3. **Falling behind:** the oldest untranscribed clip has waited too long.
  4. **Host health: disk nearly full.** CPU temperature and frequency are *shown* on the status
     page but don't raise a warning.
- **Event log:** outages and recoveries are kept about **30 days** and listed on `/status`.
- **Notifications outside the page** (phone push etc.) are a **future nice-to-have**
  (see "Later").

### The gap today

The worker publishes a heartbeat (`data/worker.json`), but the **capture process publishes
nothing**. Its connection state and reconnects exist only in its log. Status needs a shared,
queryable record from every component.

### Design

**1. Components publish their state** in the database, readable by the web process:

```sql
CREATE TABLE component_state (           -- one row per component, overwritten (capture 5 s, worker 10 s)
    name       TEXT PRIMARY KEY,          -- 'capture', 'worker'
    state      TEXT NOT NULL,             -- e.g. streaming | connecting | reconnecting | stopped
    detail     TEXT NOT NULL,             -- JSON, see below
    pid        INTEGER,
    updated_at TEXT NOT NULL              -- heartbeat; stale = process gone
);

CREATE TABLE system_events (             -- transitions only; kept ~30 days
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    component  TEXT NOT NULL,             -- capture | worker | queue | host
    level      TEXT NOT NULL CHECK (level IN ('info','warn','error')),
    kind       TEXT NOT NULL,             -- e.g. disconnected, reconnected, degraded, backlog
    message    TEXT NOT NULL,             -- human-readable, shown in the log
    detail     TEXT                       -- JSON
);
```

- **Capture** (`SDS200NetworkSource` + pipeline):
  - `state`: `connecting`, `streaming`, `reconnecting`, `session_busy` (another program holds
    the audio session) or `stopped`.
  - `detail`: connected since, last audio and last status times, reconnect count, last error,
    next retry in N s, RTP packets lost, current talkgroup.
  - Events: `disconnected` (with reason), `reconnected` (with outage length), `session_busy`,
    `started`, `stopped`.
- **Worker:** replaces `worker.json` with its row.
  - `state`: `gpu`, `degraded` or `stopped`.
  - `detail`: model, degraded reason, clips processed, current clip.
  - Events: `degraded` (entered, with reason), `started`, `stopped`. The supervisor adds
    `crashed` and `restarted`.

**2. One health evaluator** (`health.py`): a pure function over component state, queue stats
and disk usage. It returns a list of conditions (`ok` / `warn` / `error`, a message and the
component) plus an overall level. It's the single source for everything below, so the header
dot, `/status`, `/api/status`, `/healthz` and the CLI `status` command always agree.

| Condition | Warn (amber) | Error (red) |
|---|---|---|
| Capture heartbeat | — | older than 15 s (capture process not running) |
| Scanner connection | reconnecting < 60 s | reconnecting ≥ 60 s, or `session_busy` |
| Worker | `degraded` mode; any clip `failed` in the last hour | heartbeat older than 30 s (worker down) |
| Falling behind | oldest queued clip waiting > 2 min | > 10 min |
| Disk (data dir) | < 10% or < 5 GB free | < 2 GB free (capture may fail) |

Thresholds are config (`[status]`). "Reconnecting < 60 s is only amber" keeps a normal quick
blip from turning the dot red.

**3. A health monitor** in the `run` process checks every 10 s and writes `system_events` when
a *derived* condition changes level (`backlog` started/cleared, `disk_low`/`disk_ok`). If the
capture process itself dies, nobody writes, so readers treat a stale heartbeat as "capture not
running". A clean stop is recorded as `stopped`.

**4. Web surfaces:**

- **Header indicator** on every page: a dot coloured by the overall level. Its tooltip lists the
  active conditions ("Scanner reconnecting (1m 20s), next try in 8 s"), and it links to
  `/status`. It updates live via the SSE event `status.changed`, sent only when the level or a
  message changes.
- **`/status` page:**
  - Current conditions at the top.
  - **Scanner:** connected since, last audio and status, reconnects today, RTP loss, current
    talkgroup.
  - **Transcription:** mode (GPU or degraded, with reason), model, queue by status, latency
    p50/p95 over the last hour, failed clips (linked).
  - **Host:** disk free, CPU temperature and frequency, uptime.
  - **Event log:** newest first, about 30 days, filterable by component. Outages show their
    length ("Scanner disconnected 10:41 → reconnected 10:43, 2 min").
- **`/api/status`:** the same data as JSON.
- **`/healthz`:** HTTP 200 when overall is ok/warn, 503 when error. Useful for systemd or any
  future external monitor.

**5. CLI:** `status` switches to the same evaluator and gains the conditions and the last few
events.

### Retention

`system_events` older than 30 days are deleted by the M5 retention job. Until then the table
stays tiny (a handful of rows a day).

### Status tests

- Health evaluator: table-driven cases for each condition and threshold, stale heartbeats and
  the overall level.
- Capture publishing: the fake live session from the M1 tests is driven through
  connect → stall → reconnect → `session_busy`, checking the state row and the event sequence.
- Worker and supervisor: degraded mode, crash and restart produce the right rows and events.
- Monitor: backlog and disk transitions are written once per change, not every tick.
- Web: the header dot's colour for each level, `/status` rendering, `/healthz` 200/503, and an
  SSE `status.changed` on a level change.
- Live: unplug the scanner's Ethernet for about 2 minutes, then check the dot turns amber, then
  red, then green, and the event log shows the outage and its length. (This overlaps the M5
  unplug test.)

## Config (`[web]`, `[status]`)

```toml
[web]
bind = "192.168.10.200"   # M3: home LAN (Dan's choice); M5: "127.0.0.1" behind tailscale serve
port = 8080
page_size = 50
timezone = ""              # empty = system local time

[status]
reconnect_warn_s = 0       # amber as soon as reconnecting
reconnect_error_s = 60
backlog_warn_s = 120
backlog_error_s = 600
disk_warn_pct = 10
disk_warn_gb = 5
disk_error_gb = 2
```

## Packaging

New `web` extra: `fastapi`, `uvicorn`, `jinja2`. SSE is hand-rolled on `StreamingResponse` (a
few lines), so there's no extra dependency. htmx and `htmx-ext-sse` are vendored as files
under `web/static/`, with versions noted in a README there.

## Tests

- **Pages** (FastAPI `TestClient` over a DB filled with synthetic rows): live feed first
  page, filter chips, "load older" keyset, **jump-to** (`?at=` anchors correctly, with newer
  and older paging around it, the time marker, local-time parsing including a DST-change
  day, and combining with `tg`), dimmed/flag rendering, grouping of exchanges,
  detail page (including a purged-audio row), status page.
- **Change feed:** created → updated sequence; the overlap window catches a row committed late
  with an older `updated_at`; resume from `Last-Event-ID` returns exactly the missed rows; the
  talkgroup filter applies; the keep-alive is sent.
- **Audio:** full response, `Range: bytes=0-99`, open-ended ranges, 416 for an invalid range,
  410 for a purged clip.
- **Status:** see the tests under System status above.
- **Manual:** phone (Safari) and desktop over the LAN during a live `run`; a network blip and
  phone sleep → no gaps; the scanner unplug test.

## Order of work

1. **Status plumbing, no UI:** migration (`component_state`, `system_events`); capture and
   worker publishing; `health.py` + the monitor; CLI `status` switched over.
2. **Web skeleton:** the `web` extra and command, the app, the base template with the
   **header status dot**, vendored static files, `/healthz`, `/api/status` and the **`/status`
   page**.
3. **Live feed:** first page, load older, **jump to date/time**, talkgroup chips, dim/hide,
   exchange grouping.
4. **Change feed:** `/events` + htmx live updates, pause pill, `status.changed` → dot.
5. **Detail page + audio** (Range, download).
6. **Tests + live checks** on phone and desktop; the unplug test.

## Step 1 notes (done 2026-09-30)

- Tables `component_state` and `system_events` (migration 0003); `statestore.py`;
  `health.py` (`assess()` is pure and table-tested); `monitoring.py` (`CaptureReporter`,
  `HealthMonitor`). The worker's `worker.json` file was replaced by its `component_state`
  row.
- **Behaviour change:** if another local process holds the audio lock (e.g. `record`), `run`
  no longer exits. It reports `session_busy` and retries every 30 s. If the scanner refuses the
  RTSP request, that's also `session_busy` (retry every 30 s) rather than a fast reconnect
  loop.
- The capture heartbeat is written every **5 s** (worker every **10 s**) by a timer,
  independent of audio, so it keeps going while the source is stuck reconnecting. Connection
  changes are written immediately. Derived conditions are checked every 10 s. `component_state`
  is one overwritten row per component, so it never grows; only transitions go into
  `system_events`.
- **SSD writes:** heartbeats were at 2 s / 5 s with SQLite's default `synchronous = FULL`,
  which was an estimated 0.5–1 GB/day of writes. Dan raised it, and it was cut to 5 s / 10 s
  with `PRAGMA synchronous = NORMAL` (standard for WAL: batched flushes, no corruption risk,
  a power cut may lose the last second or so of commits).
- One event per outage (start and end), not per retry. The very first `connecting` at startup
  isn't counted as an outage.
- The CLI `status` shows the overall level, active conditions, capture/worker/host detail and
  recent events. It was checked live: OK while running, ERROR with reasons after stopping.

## Step 2 notes (done 2026-09-30)

- `web` command (`web/server.py`, Jinja2 templates, `static/app.css`); htmx 2.0.11 +
  htmx-ext-sse 2.2.4 vendored (0BSD, hashes in `static/README.md`). The `web` extra is
  fastapi, uvicorn, jinja2. The test client uses `httpx2` (Starlette deprecated `httpx`).
- Header dot on every page: green/amber/red, the tooltip lists the conditions, and it links to
  `/status`. On phones the label hides and the dot grows. **It polls
  `/fragments/status-dot` every 10 s until step 4 switches it to SSE.**
- `/status`: overall conditions, Scanner / Transcription / Host cards, and the 30-day event
  log with component filter chips. `/healthz` (200 ok/warn, 503 error). `/api/status`.
- Times are shown in `[web] timezone` (default: system local). Dan's `config.toml` binds
  `192.168.10.200:8080` (home LAN, as chosen); the example defaults to 127.0.0.1.
- Running for Dan in detached screen sessions `scanner-run` and `scanner-web`
  (`screen -r scanner-web`, Ctrl-C to stop).

## Step 3 notes (done 2026-09-30)

- `web/feed.py`: keyset queries on `(started_at, id)` (`latest` / `older` / `newer` /
  `around`), URL-safe cursors `<epoch_ms>_<id>`, and row presentation (labels, flags,
  exchange grouping, day breaks).
- **Exchange blocks (redesigned after Dan's screenshot):** each run of transmissions on one
  talkgroup (gaps under 10 s) is its own block. A header strip names the talkgroup once (name,
  department, TG), compact rows sit beneath (time, play, transcript; unit and duration quiet
  on the right), and there's space between blocks. The first attempt hid the label per row,
  which left blank gaps and ran one rule through different talkgroups.
- **Talkgroup colours:** each block's edge (and its chip's swatch) uses the talkgroup's own
  colour. It's stored in `channels.color` (migration 0004), assigned in first-heard order from
  a 10-hue palette with no red, since red means "problem" here. The first ten talkgroups
  always differ; hashing the TGID had collided for Sheriff and 11B. The name is always shown
  too, so colour is never the only cue. It's editable later in M4.
- Chips add the department when names collide ("Dispatch *County Sheriff*" vs "Dispatch
  *Town Police*").
- Checked visually with headless Firefox screenshots (desktop light/dark, phone dark).
  `firefox --headless --screenshot` works here; snap confinement means writing under
  `~/snap/firefox/common/`.
- **Step 4 must handle** a new row joining the top exchange: insert it right after that
  block's header instead of adding a new header.
- **Jump to:** native `datetime-local` picker plus Quick shortcuts; `/?at=…` anchors with a
  marker, "Newer" above and "Load older" below, and "Back to live". Local time is parsed with
  the OS rules (or `[web] timezone`); a repeated DST hour means its first occurrence, and a
  nonexistent one is shifted, never an error. A purged-audio note is shown if relevant.
- Talkgroup chips (heard in the last 24 h, with counts), toggled in the URL (`?tg=`), combine
  with jump-to. "Hide no-speech" is a per-browser toggle (`localStorage`).
- **Play buttons** use one shared `<audio>` element (`static/app.js`). The basic audio
  endpoint (Range via Starlette, 404/410, path confined to the data dir) landed early so they
  work; step 5 adds download names and the Safari check.
- The time link on each row goes to `/t/{id}`, which 404s until step 5.
- Real data: the live page renders 50 rows in about 70 ms; a jump in about 10 ms.

## Step 4 notes (done 2026-09-30)

- `/events` (one stream per page): `tx` events on the live feed (rendered row + exchange
  header + metadata) and `status` events for the header dot on every page (health
  re-checked every 5 s, sent only on change). There's a 15 s keep-alive and `retry: 3000`.
- `web/changes.py` `ChangeFeed`: polls `updated_at` every 0.5 s with a **2 s overlap window**
  and (id, updated_at) de-duplication, because two processes commit with pre-commit
  timestamps. Event id `<updated_at>|<id>`. Resume order: `Last-Event-ID` header, then
  `?last=` (fresh EventSource after phone sleep), then `?since=` (page render time, so there's
  no gap between render and connect).
- Browser (`static/app.js`, native `EventSource`; the htmx SSE extension was dropped):
  - existing rows are **replaced in place** (the transcript arrives), keeping the playing state;
  - new rows join the top exchange (inserted under its header) or start a new block, adding a
    day divider when the date changes;
  - rows older than the top are left for a reload;
  - while **paused** or scrolled more than 300 px down, new rows wait behind a "N new — show"
    pill; scrolling back up or tapping the pill shows them;
  - on `visibilitychange` the stream is reopened, resuming from the last event.
- **Bug found and fixed:** `web` hung on Ctrl-C while any page was open, because uvicorn waits
  for streaming responses. Now `timeout_graceful_shutdown=3`.
- **Real-browser checks** (`tests/ui/`, headless Firefox + Selenium against a scratch
  instance): 18 checks pass (new row, transcript swap, joining an exchange, new block,
  pill when scrolled, pause/resume, dot leaving red, no duplicates, no JS errors). The
  reconnect check passes too (server restarted mid-session; rows written meanwhile arrive,
  no duplicates).
- Live: a real transmission arrived as "transcribing…" and updated in place within seconds.

## Step 5 notes (done 2026-09-30)

- `/t/{id}`:
  - a talkgroup-coloured header card with the player (`preload="metadata"`) and a
    **Download** button;
  - the transcript, large, with flags in plain words; the model's original output and the
    second model's text when they differ;
  - Transmission / Transcription cards: local and UTC times, units, site, frequency, signal,
    continuation or status-missing notes; model, processing time, latency after the
    transmission ended, attempts, errors;
  - **"On this talkgroup around then"**: up to 5 before and after, chronological, with
    Previous/Next;
  - collapsible raw data (status changes, the scanner's raw XML);
  - "Back to the feed at HH:MM" (a jump-to link);
  - a friendly 404.
- While a transcript is pending, the text block polls `/fragments/tx-text/{id}` every 3 s.
  The server answers **HTTP 286** once it's done, which tells htmx to stop polling.
- `/api/transmissions/{id}` JSON (no filesystem path; `audio_url` instead).
- Audio: `?download=1` sends `Content-Disposition: attachment;
  filename="YYYY-MM-DD_HHMMSS_TG1101_<id>.wav"` (local time). Range behaviour is tested:
  `bytes=0-1` (Safari's first probe), open-ended, suffix and out-of-range (416). **Real
  Safari is still to be checked on Dan's iPhone in step 6.**
- Definition lists stay two-column on phones (labels are short). Checked with headless
  Firefox screenshots.

## Live row cap (added 2026-09-30, Dan's request)

The live feed keeps at most `[web] live_max_rows` (300) rows in the browser. When a new row
pushes it over, the oldest rows are dropped from the bottom, along with any header or day
divider left empty, and the list ends with "Load older" at the new oldest row. Trimming only
happens at the top of the live view (never while paused or scrolled down reading); jump-to
and detail pages are unaffected. Browser check: `tests/ui/row_cap_check.py`.

## Pending tests

- [ ] **Scanner unplug test** (flagged 2026-09-30; Dan away from home). Pull the SDS200's
  Ethernet for about 2 minutes while watching the web page. Expect: the dot turns amber
  ("Scanner unreachable … reconnecting") within about 5–10 s, then red after 60 s;
  `/status` shows the error and the next retry. After reconnecting: green again, and the
  event log shows "Scanner connection lost" → "Scanner reconnected after 2m …". Also check
  that no transmissions are duplicated and capture resumes (new rows appear). This overlaps
  the M5 reconnect tests.
- [ ] **iPhone Safari audio:** confirm the play buttons and the detail-page player work
  (Range support is covered by automated tests).

## Later (not M3)

- **Listen-along auto-play** (Dan: later): moved into M5 phase A2 as "continuous playback"
  (catch up + listen along); see `docs/m5-plan.md`.
- **Notifications when you're not looking** (nice-to-have): phone push via ntfy.sh, or email,
  on outage and recovery (e.g. scanner unreachable more than 5 min, worker down, disk low).
  They would hang off `system_events` with no new detection logic. Candidate for M5.
- **Tailscale** (`tailscale serve`, bind to localhost): M5, as planned.
- **Search and channel aliases/priority/ignore editing:** M4. Search keeps its from/to
  date-range filter, which complements jump-to (a window of results vs. browsing the timeline).
