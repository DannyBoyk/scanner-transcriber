# M5 plan: finish, review, deploy, prove

**Status:** Phase A **code complete** (2026-09-30): retention and continuous playback built,
tested and running (both services restarted; the first retention pass ran live: "nothing to
remove"). Phase B **done** (2026-09-30): findings, triage and fixes are in
[`m5-code-review.md`](m5-code-review.md). The running services still run the pre-review
code; they pick up the fixes (and migration 0007) at the Phase C deployment. Phase C **done**
(2026-09-30): both services run under systemd (sandboxed, watchdog, restart with backoff);
cutover lost ~4 s of audio; graceful restart (SIGTERM, like `systemctl restart`) → TEARDOWN,
back in ~13 s; `kill -9` → systemd restarted capture in 6 s, **but the scanner then refused
port 554 until power-cycled** (see "Later"). Web still binds 192.168.10.200 until Tailscale
is on the NUC. Next: Phase D.

**Goal:** the logger runs unattended on the NUC: it starts on boot, recovers from scanner,
network and process failures without help, keeps the disk bounded, and has been reviewed and
tested end to end.

**Order (Dan):** **A.** get code complete (retention, continuous playback) → **B.**
comprehensive code review and fixes → **C.** deploy as system services → **D.** resilience
and soak testing.

## Decisions (Dan, 2026-09-30)

| Topic | Decision |
|---|---|
| Service account | **Dan's user** (`user`), running from the existing repo, with systemd hardening. Models, data and config stay where they are. |
| Audio retention | **90 days** (about 9–15 GB at current traffic). |
| Transcript retention | **365 days**: rows (transcript, metadata, search) older than a year are deleted. Measured about 3.7 KB per transmission, which is about 2.7 GB/year at today's ~2,000/day, or 2.5–4 GB allowing for busier periods. |
| Raw scanner data | **Trimmed at 90 days**, with the audio: `raw_status` (about 1 KB XML) and `status_changes` (about 0.5 KB) are cleared but the row is kept. That's about 40% of each row, so it roughly halves the database, to about 1.5–2 GB/year. The data is only useful for diagnosing recent problems. |
| Backups | **None** for now. |
| Alerts (phone/email) | **Later**, after M5. They'll hang off `system_events`. |
| Tailscale | Not needed: Dan already reaches the web page through a Tailscale subnet router. The web keeps binding to the LAN address. |
| Continuous playback | **Catch up on a period** and **listen along live** (not per-conversation). **Desktop first**: clips are chained in the browser. Back-to-back with short pauses. When behind live: show how far behind plus "Jump to live", never skip automatically. A server-side continuous stream (Broadcastify-style, works with the phone locked and in external players) is **noted for later**. |

---

## Phase A: code complete (retention)

**Retention job**, running inside `run` every hour (and once shortly after start):

- **Audio:** for rows whose `started_at` is older than `retention.audio_days` (90), delete the
  WAV and set `audio_path = NULL` (row and transcript kept). It selects by the row's
  timestamp, **not folder names** (folders are local dates; see the M1 notes). Empty day,
  month and year folders are removed. It works in batches (e.g. 500 per transaction) so a big
  first run doesn't hold locks for long.
- **Raw scanner data:** for rows older than `retention.raw_days` (90), clear `raw_status`
  and `status_changes` (the row, transcript and metadata stay).
- **Transcripts:** delete rows older than `retention.transcript_days` (365). The FTS triggers
  remove them from both search indexes. Channel `first_heard_at` / `last_heard_at` are
  unaffected. Rows in a re-transcribe batch or referenced by a prompt test are deleted
  normally (batches and tests keep their summaries).
- **Events:** delete `system_events` older than `retention.event_days` (30).
- **Commands:** delete finished `commands` older than 30 days.
- **Prompt test results:** keep the newest `retention.prompt_tests_keep` (100) results and
  delete older ones. Each is about 2 KB (score, the prompt tested, the 10 worst clips); the
  tests themselves leave nothing else behind (clips are transcribed in memory). Plus a
  **Delete** button on each result in the Admin prompt-test list (login required, same
  guard), for tests not worth keeping.
- **Never touched:** `prompt_versions`, `retranscribe_batches`,
  recorded sessions under `data/sessions/` (the labelled clips the prompt test uses), and logs
  (already rotating).
- **Housekeeping after each run:** `PRAGMA wal_checkpoint(TRUNCATE)`, and `PRAGMA optimize`
  once a day. Deleted rows' space is reused by SQLite but the file doesn't shrink, which is
  fine because it plateaus at about a year of data. A **`VACUUM`** runs in a quiet hour (e.g.
  03:00) when more than about 20% of the file is free pages, and never while the disk is too
  full to hold a temporary copy.
- **Safety:** a configured value of 0 means "keep forever". There's a dry-run CLI:
  `python -m scanner_transcriber retention --dry-run` prints what would be deleted and how
  much space it frees. Each real run writes a `system_events` entry ("Retention: removed
  audio for 1,204 clips older than 90 days, freed 180 MB").
- **UI:** the detail page and feed already handle purged audio ("removed by retention").
  `/status` shows the retention settings and the last run.

```toml
[retention]
audio_days = 90          # clip WAVs; 0 = keep forever
raw_days = 90            # raw scanner status XML / status changes on each row; 0 = keep
transcript_days = 365    # whole rows (transcript + metadata + search); 0 = keep forever
event_days = 30          # status-page event log
prompt_tests_keep = 100  # newest Admin prompt-test results kept; 0 = keep all
run_every_minutes = 60
```

**Tests:** age selection by `started_at` (including local-vs-UTC folder dates and DST); file
and row consistency; a missing file is tolerated; batching; 0 = keep forever (each setting);
raw trim keeps the transcript; row deletion removes it from both search indexes; dry run
changes nothing; sessions and labelled clips untouched; event and command pruning; prompt-test cap and the Delete button (auth + guard); the VACUUM
conditions (free-page ratio, disk space, quiet hour); the status page shows the last run.

**A1 done (2026-09-30):** `retention.py`, `[retention]` config, hourly loop in `run` (first
pass 2 minutes after start), `retention [--dry-run]` CLI, Storage details on `/status`, and a
Delete button on prompt-test results. In-flight (queued/processing) rows are never touched.
Dry runs skip rows that the row-deletion step would remove, so nothing is double counted. The
dry run on the real DB reported "nothing to remove". 10 tests. Not yet running live: it starts
with the next restart of `run`.

### A2: Continuous playback ("play everything")

One player for two uses: **catch up** (a jump-to view, search results, a talkgroup filter) and
**listen along** (the live feed).

- **Start:**
  - **"Play from here"** on each row: a small ⏵⏵ button beside ▶, shown on hover on desktop
    and always on touch screens;
  - **"Play all"** in the controls: starts from the oldest row shown.
- **Order:** chronological, oldest → newest. On the newest-first page that means playback
  moves up the list.
- **Queue:** built from the rows' data (clip URL, talkgroup, time, transcript), not tied to
  the page elements. So the live row cap trimming the bottom, or the "N new" pill holding rows
  back, doesn't disturb playback.
  - **No-speech clips are skipped.** Clips still being transcribed are played (their audio
    exists).
  - The page's talkgroup filter applies; hidden talkgroups stay hidden.
- **Gaps:** about 0.5 s between clips and about 1.5 s when the talkgroup changes, so the
  boundary is audible.
- **End of what's shown:**
  - on the **live feed**, it waits for new transmissions (from the SSE stream) and plays them
    as they arrive: listen-along;
  - on **jump-to and search** pages, it loads the next newer page automatically and carries
    on; at the very end it stops.
- **Behind live:** the bar shows "3m 20s behind live" when the clip playing is more than about
  30 s old while listening along, with a **Jump to live** button. Nothing is skipped
  automatically.
- **Player bar**, pinned to the bottom of the page while playing:
  - play/pause, previous, next, stop;
  - **speed** 1× / 1.25× / 1.5× / 2× with pitch preserved, remembered per browser;
  - what's playing: talkgroup colour, name, time, transcript (links to the detail page);
  - the behind-live indicator.
- **Following:** the playing row is highlighted and scrolled into view, unless you've
  scrolled yourself in the last few seconds (then it leaves the page alone until you tap
  "Follow").
- **Desktop:** keyboard shortcuts (Space play/pause, ←/→ previous/next, Esc stop, ignored while
  typing), and the **Media Session API** so keyboard media keys and the browser's media panel
  show the talkgroup and transcript.
- **Phones:** works with the screen on. With the screen locked, playback may stop after the
  current clip. That's accepted for now; see "Later: continuous stream".

**Tests:**
- unit tests of the queue logic, as JS pure functions: order, skips, filters, pauses, the
  behind-live calculation;
- page tests for the new controls and data attributes;
- browser checks (`tests/ui/player_check.py`, headless Firefox against a scratch instance):
  - "Play all" plays in chronological order;
  - no-speech skipped;
  - a talkgroup change adds the longer pause;
  - speed applied;
  - listen-along picks up a newly inserted clip;
  - behind-live appears, and Jump to live works;
  - auto-loading the next page on search;
  - keyboard shortcuts;
  - no JS errors.

Audio output can't be heard in headless tests, so they check the `<audio>` element's source
sequence and playback events.

**A2 done (2026-09-30):**
- `static/player.js` owns the single `<audio>` (the ▶ single-clip buttons too);
  `_player_bar.html` is in the base layout; rows carry the data the player needs (`data-src`,
  `data-label`, `data-hue`, `data-text`, `data-nospeech`); a ⏵⏵ button on each row and "⏵⏵
  Play all" on the live, jump-to and search pages.
- Gaps are divided by the playback rate when faster than 1×.
- `tests/ui/player_check.py`: 13 checks (order, no-speech skip, longer talkgroup pause,
  listen-along, speed, Esc, play-from-here, behind live + jump, Space, jump-to auto-load, no
  JS errors) against a scratch instance with `page_size = 4`. The live-updates check was
  rerun: 18/18 pass.

**Exit:** all tests green; a dry run on the real DB prints a sensible (currently empty)
retention plan; continuous playback checked in a real browser. That's **code complete**:
no new features after this point.

---

## Phase B: comprehensive code review

A deliberate, whole-codebase review **after** code complete and **before** deployment. It's
not a diff review: it goes module by module across the capture, segmenter, worker, DB, web and
admin code.

**Review dimensions:**

1. **Correctness:** edge cases in the segmenter (timeline gaps, unit handoff, max-length
   splits), change feed (overlap window, resume), keyset paging, jump-to and DST, retention.
2. **Concurrency and SQLite:** two writer processes plus web readers, transaction boundaries,
   `busy_timeout`, long transactions, the WAL checkpoint, claim atomicity, SSE threads.
3. **Resource handling:** RTSP TEARDOWN on every exit path (including a crash inside
   callbacks), sockets, file handles, subprocesses, the worker's GPU memory, generator
   cleanup.
4. **Security:**
   - auth and session cookie;
   - rate limiting;
   - the cross-site guard;
   - XSS: every place transcripts, scanner strings or user input reach HTML;
   - path traversal (audio, logs);
   - SQL built with f-strings (check that all values are parameterised);
   - LAN exposure with no TLS.
5. **Failure behaviour:** what happens when the DB is locked, the disk is full, the GPU
   disappears mid-run, the scanner sends malformed XML, or the clock jumps (NTP).
6. **Performance at scale:** a year of data (about 50k–100k rows). Query plans for the feed,
   search, channel counts and status. Index coverage. FTS size.
7. **Test gaps:** important paths without tests; brittle tests.
8. **Maintainability:** dead code, duplication (e.g. `add()` helpers, template fragments),
   inconsistent naming, docs that no longer match the code.

**How:**

- I run it systematically, dimension by dimension, reading every module. I verify each
  finding by reproducing it with a test or a concrete scenario, and rank findings by
  severity.
- The findings go to Dan as a **list to triage** (fix now / later / won't fix) **before
  anything changes**. Then I fix, add regression tests, and rerun the full test suite and
  browser checks.
- **Optional extra:** Claude Code's own `/code-review ultra` runs a separate multi-agent
  review in the cloud. It's **started by Dan** (it's billed separately), and its findings can
  be merged into the same triage list.

**Exit:** every "fix now" item fixed with a regression test; 100% of pytest and all
`tests/ui` browser checks passing; docs updated to match the code.

---

## Phase C: deploy as system services

**Units** (in `deploy/`, installed with a script; both `User=user`, working directory =
repo, running `.venv/bin/python`):

| Unit | Runs | Notes |
|---|---|---|
| `scanner-transcriber.service` | `run`: capture + supervised worker | `After=network-online.target`; `SupplementaryGroups=render video` (iGPU); `TimeoutStopSec=30` (the worker finishes its clip, then TEARDOWN) |
| `scanner-transcriber-web.service` | `web` | Independent of capture, so it keeps reporting status when capture is down |

- **Restart policy:** `Restart=on-failure`, `RestartSec=5`, with a start-rate limit so a
  crash loop backs off instead of hammering the scanner.
- **Hang detection:** systemd's **watchdog** (`WatchdogSec=60`). The capture heartbeat loop
  pings it (`sd_notify WATCHDOG=1`, a few stdlib lines, no dependency), so a hung process
  (not just a crashed one) is restarted.
- **Hardening:**
  - `NoNewPrivileges`, `PrivateTmp`;
  - `ProtectSystem=strict`, with `ReadWritePaths=` only for the repo's `data/`, the OpenVINO
    cache and the HF cache;
  - `ProtectHome=read-only` otherwise;
  - `DevicePolicy=closed` with `DeviceAllow=/dev/dri` only for capture;
  - `RestrictAddressFamilies` limited to what's used (inet/unix/netlink).
- **Logs:** the existing `data/logs/*.log` stay (for the Admin viewer). stdout also goes to
  journald (`journalctl -u scanner-transcriber`).
- **Cutover:** stop the `screen` sessions (with Dan, **not while a prompt test runs**),
  enable and start the units, then check status, the web page and a live transmission.
- **Boot:** `systemctl enable`. Verify with a real reboot in phase D.
- **Updates:** `deploy/update.sh` pulls, syncs dependencies, runs the tests, and restarts
  only if they pass. Restarting capture costs a few seconds of audio; that's documented.
- The README's "running" section switches from `screen` to `systemctl` / `journalctl`.

**Exit:** both services running under systemd, surviving `systemctl restart` and a manual
`kill -9` (restarted automatically), with the web and status correct throughout.

---

## Phase D: resilience and soak tests

Each test lists **what to do → what should happen**, recorded in a checklist in this file.
All of them need Dan at the NUC.

1. **Scanner Ethernet unplug, about 2 min** (pending since M3) → dot amber then red, then
   green after reconnecting; the event log shows the outage with its length; no duplicate
   rows; capture resumes.
2. **Scanner power-cycle** → same, plus a clean reconnect after the scanner boots (the RTSP
   session is fresh).
3. **`kill -9` of capture** (no TEARDOWN possible) → systemd restarts it. Important: check how
   the SDS200 treats the orphaned RTSP session. Design risk R2 warned it might refuse a new
   session until power-cycled. Expected: `session_busy` retries every 30 s until the scanner
   times the old session out. Measure how long that takes.
4. **Network glitch on the NUC** (`ip link set eno1 down` for 30 s) → reconnect, no manual
   steps.
5. **NUC reboot** → both services up on their own, scanner reconnected, worker on the GPU,
   no manual steps.
6. **Web restart while pages are open** → pages reconnect and catch up (checked in M3 on a
   scratch instance; now for real).
7. **Disk-space warning** (simulated by lowering the threshold) → amber dot and event, then
   cleared.
8. **24-hour soak with real traffic** → watch CPU temperature and throttling, memory growth
   of each process, queue latency p50/p95, disk growth versus the retention estimate, and
   reconnect counts. The report goes into this file.
9. **Busy-period recording** (from M0: record a busy night, e.g. a Friday) → confirms the
   single worker keeps up (the RTF ≤ 2 target and the clips-per-minute capacity).
10. **iPhone Safari audio** (pending since M3) → play buttons and the detail player work.

**Exit:** all checks pass or have a documented, accepted explanation; soak report written.
M5 done means the system is **deployed and proven**.

### Checklist (results)

| # | Test | Date | Result |
|---|---|---|---|
| 1 | Scanner Ethernet unplug | 2026-09-30 | **Mixed, explained.** About 25 s: recovered by itself 9 s after the replug. About 2 min: scanner locked out (554 refused) until rebooted. Cause found: the NUC's kernel stops retrying our FIN about 50 s after the close. `MSM,0` over UDP reboots the scanner and restores audio. Details in [`sds200-rtsp-lockout.md`](sds200-rtsp-lockout.md). Status page and dot followed the outage correctly. **Then fixed (prevention: parked TEARDOWN + reset-on-close; recovery: automatic `MSM,0`) and re-tested:** a 2.5-min unplug recovered by itself 1 min 24 s after the replug, with no reboot. In an earlier run, the automatic reboot recovered a lockout in 22 s. |
| 2 | Scanner power-cycle | 2026-09-30 | **Pass.** Done as the recovery from #3: while the scanner booted, capture saw "Timed out waiting for MDL", then reconnected by itself at the next retry ("Scanner reconnected after 16m 09s" in the event log). No duplicate rows. |
| 3 | `kill -9` of capture | 2026-09-30 | **Accepted known issue.** systemd restarted capture in 6 s, but the SDS200 refused TCP on 554 (not the expected RTSP 453 / `session_busy`) until power-cycled: still refused after 16 min. Fix deferred (see "Later"). Status page showed the outage correctly; no duplicate rows. |
| 4 | `ip link set eno1 down` 30 s | 2026-09-30 | **Skipped:** the same mechanism as #1 (link loss → FIN timing), covered there. |
| 5 | NUC reboot | 2026-09-30 | **Pass** (Dan): rebooted at 20:12; both services came back by themselves, capture reconnected and the worker was on the GPU, with no manual steps. |
| 6 | Web restart with a page open | 2026-09-30 | **Pass.** Live page open in headless Firefox; SIGTERM to the web (systemd restart) → back in 9 s; the page reconnected and showed the next real transmission once, without a reload. |
| 7 | Disk-space warning | 2026-09-30 | **Pass.** Scratch `run --replay --speed realtime` with `disk_warn_gb = 1000`: the running process's monitor wrote a `disk_low` warn event, and `/healthz` and the dot listed "Disk getting full". Clearing is covered by `test_monitoring.py`. Note: the monitor's state is in memory, so a condition that clears across a capture restart gets no "cleared" event (the dot is still right). |
| 8 | 24-hour soak | 2026-10-01 → 10-03 | **Pass.** 47 h of real traffic (stopped early at Dan's request, to reprogram the scanner). No restarts, no reconnects, no lost audio, no failed clips, no throttling, no memory growth. See the soak report below. |
| 9 | Busy period | 2026-10-01/02 | **Pass.** The busiest hour (Thu 15:00, 327 transmissions, 17.7 min of speech) used 68% of the worker. Friday night (18:00–24:00, 879 transmissions) peaked at 51%. Nothing ever waited more than 7 s in the queue. |
| 10 | iPhone Safari audio | 2026-09-30 | **Pass** (Dan): playback works. |

**Levelled audio for transcription? Tested, no.** (2026-09-30) Playback now levels clips to
about -18 dBFS (they're recorded at about -30). We ran the worker's exact path on the 72
hand-labelled clips, as recorded vs levelled, interleaved: WER 0.263 vs 0.264, CER 0.184 vs
0.189. 61 clips were identical, 5 better and 6 worse by a word or two. The worker keeps
transcribing the audio as recorded; levelling stays listening-only.

The soak (#8) was started on 2026-09-30 at 20:14, then stopped and cleared at Dan's request.
**Restarted fresh on 2026-09-30 at 22:11** (`data/soak/soak-20260930-2211.csv`, 72 h, until
2026-10-03 22:11), right after the deploy and a real **Clean start** test from the Admin page.
The test removed 1,590 transmissions, 76 MB of audio, 105 events and 3 log files in 5 s.
Talkgroups, prompts, labelled clips, prompt tests and the password were kept, and capture
kept streaming. The safety copy taken beforehand was deleted (Dan: fine to let the old data
go).

### Soak report (#8, #9)

**Run:** 2026-10-01 13:38 → 2026-10-03 12:43 EDT, 47 h, sampled every 5 min (566 samples,
`data/soak/soak-20261001-1338.csv`, `deploy/soak.py --report`). Planned for 72 h; Dan ended it
early to update the scanner's talkgroups in Sentinel. Real traffic throughout: **5,452
transmissions, 4.7 h of speech, 24 talkgroups.** The services ran for the whole soak (up since
Thu 12:00, `NRestarts` 0); the web restarts of Wed/Thu were before it started.

| Measure | Result |
|---|---|
| Capture | `streaming` in every sample; 0 reconnects, 0 lost RTP packets |
| Worker | GPU mode in every sample; 0 failed clips |
| CPU | 30–50 °C (median 34), 0 throttle events, load median 0.28 (max 1.9) |
| Queue | at most 3 clips waiting, oldest at most 7 s |
| Latency (end of speech → transcript) | p50 median 7.2 s; p95 median 9.8 s, worst 30.5 s |
| Worker busy | median 14%, peak 68% (Thu 15:00) |
| Memory available | never below 2.6 GB (of 7.0) |
| Database | 5.3 → 26.2 MB (about 10 MB/day) |
| Audio | 273 MB of clips in 47 h (about 140 MB/day); disk 209 → 208.2 GB free |

- **Latency.** The worst p95 (30.5 s, Sat 10:43) was a single 36-second transmission, which
  took 28.7 s to transcribe (Whisper processes it as two 30-second windows). It wasn't a
  backlog: the queue held at most 3 clips. Short clips still came back in about 7 s.
- **Memory: no leak.** Worker 1,310–1,332 MB, web about 180 MB, capture 70–90 MB. All
  three were flat from Thursday evening on. At about 05:15 on Saturday, all three dropped
  together (worker 1,322 → 1,106 MB) and stayed flat at the lower level. This wasn't a
  restart. The daily apt update ran at 05:15 (peak 437 MB) and the kernel moved some idle
  pages to swap (worker: 75 MB swapped). It's harmless; a lower RSS doesn't mean less is in use.
- **Busy periods (#9).** The busiest hours, by transmissions:

  | Hour | Transmissions | Speech | Worker busy | Latency p50 / p95 / max |
  |---|---|---|---|---|
  | Thu 15:00 | 327 | 17.7 min | 68% | 7.3 / 12.3 / 15.9 s |
  | Thu 17:00 | 259 | 12.1 min | 61% | 7.3 / 13.3 / 17.2 s |
  | Fri 16:00 | 213 | 13.2 min | 50% | 7.2 / 10.9 / 15.2 s |
  | Fri 21:00 | 210 | 10.5 min | 51% | 7.3 / 11.2 / 17.6 s |

  The single worker has about 1.5× headroom over the busiest hour seen. Bursts within the
  hour (a 5-minute peak of 38 clips) cleared without a backlog.
- **Disk vs retention.** At about 140 MB/day of audio, 90-day retention settles near
  13 GB; the database, at 10 MB/day with 365-day transcripts, near 4 GB. 208 GB is free.
  (The disk-free figure fell faster than the clips grew, by about 0.5 GB: system logs and
  updates, not this app.)

**M5 exit met:** every Phase D check passes or has a documented, accepted explanation
(#3 `kill -9` lockout: accepted, fix deferred; #4 covered by #1). **M5 is done.**

Small things noticed (later): the outage event keeps the first error ("timed out") even when
it becomes "connection refused"; a `kill -9` leaves no "capture crashed" event (only the next
"Capture started").

## Later (after M5)

- **Continuous stream, Broadcastify-style:** now planned as M6, see
  [`m6-listen-live-plan.md`](m6-listen-live-plan.md). Original note (Dan, 2026-09-30): The server
  stitches clips into one continuous audio stream (per talkgroup filter, e.g. an MP3 or Opus
  stream at `/stream?tg=…`). It plays like a radio station, keeps going with a phone locked
  or in the background, shows lock-screen controls, and works in external players (VLC,
  car). The browser player from A2 stays for desktop catch-up.
- ~~SDS200 audio lockout: prevention + automatic `MSM,0` recovery~~ **Done in phase D**
  (2026-09-30, see [`sds200-rtsp-lockout.md`](sds200-rtsp-lockout.md)). Automatic recovery
  also covers the `kill -9` case below (reboot about 3 min in). The FD-store idea is only
  needed if a reboot-free recovery from process deaths matters.
- **Survive an abrupt capture death without locking the scanner** (found in phase C,
  2026-09-30; Dan: low priority, fix and test later). After `kill -9` of `run`, no TEARDOWN
  was sent and the SDS200 **refused TCP connections on 554** (not an RTSP 453) until it was
  power-cycled: still refused after 5+ minutes. Graceful stops (`systemctl stop/restart`,
  reboot, Python exceptions) are fine. Abrupt deaths that would trigger it: OOM killer,
  native crash, and the systemd watchdog (it kills a hung process). Idea: hand the RTSP TCP
  socket to systemd's file-descriptor store (`FDSTORE=1`, `FileDescriptorStoreMax=1`) and
  save the session ID in `data/`; the restarted process gets the socket back, sends the
  missed TEARDOWN over it, then reconnects. The `sds200` `RtspClient` accepts a custom
  connection factory, so it's feasible (about half a day). Test: `kill -9` the main PID and
  confirm audio resumes without a power cycle. Doesn't cover NUC power loss or an unplugged
  cable (phase D will show whether those also lock the scanner); backstop for those: a smart
  plug to power-cycle the scanner, and a Status-page hint ("power-cycle the scanner") after a
  few minutes of "connection refused".
- **Listen live on cellular / CarPlay** (2026-10-01): the stream can stop after a cellular
  hiccup while the phone is locked. Options: a bigger head start, or HLS (the proper fix). See
  [`m6-listen-live-plan.md`](m6-listen-live-plan.md), "Known issue and future work".
- **Transcription accuracy ideas** (parked 2026-10-01): pick from a known list for short
  replies and bare unit IDs, a county-name prompt tweak, earlier clip starts. See
  [`stt-accuracy-2026-10.md`](stt-accuracy-2026-10.md).
- Phone/email alerts (ntfy), off-box backups, keyword alerts, export, a better model for
  re-transcribing on demand.
