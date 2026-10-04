# M5 Phase B: code review findings (2026-09-30)

A whole-codebase review against the eight dimensions in `m5-plan.md`, done before deployment.
I checked each finding with a test, a measurement or a concrete scenario. The scratch checks
are not in the repo yet; each one becomes a regression test when its finding is fixed.

**Performance baseline.** I built a synthetic year of data: 730,000 rows and a 0.76 GB
database. Audio paths and raw XML are kept only on the last 90 days, as retention would
leave them. Today's real rate is about 100 transmissions an hour in the daytime, so a year
is about 0.5–0.7 million rows. The plan assumed 50–100k (see D2).

Severity levels:
- **High:** can stop capture or transcription, or silently break the live page.
- **Medium:** a real defect with a narrower trigger, or a slow page.
- **Low:** hygiene, hardening or cosmetic.

## High

### H1. One failed database write stops all capture
`app.run_pipeline` calls `ClipStore.save` with no error handling. (Correction during the
fixes: the reporter's writes from the source's callbacks were already guarded. Only the
"capture stopped" write at shutdown wasn't, and it could hide the real reason for stopping.)

- **Trigger:** any write that fails, such as "database is locked" (a lock held for more than
  5 s, see M1) or a full disk.
- **Effect:**
  - `run` exits, and it takes the worker process with it.
  - The clip being saved is lost.
  - Everything said until something restarts `run` is also lost.
- **Verified:** a test that makes the first save raise `OperationalError`. The pipeline
  stopped, and a later transmission in the same replay was never captured.
- **Fix:**
  - Retry a failed save a few times with a short backoff. If it still fails, log it, record
    a system event and keep capturing. The WAV could go to a spill folder.
  - Handle errors from the reporter's writes the same way.
  - systemd's `Restart=` (Phase C) is a backstop only.

### H2. After a bulk update, open live pages stop showing new transmissions
The SSE change feed reads at most 500 rows per poll, starting 2 s before its cursor. It skips
rows it has already sent.

- **Trigger:**
  - A re-transcribe batch (up to 2,000 rows).
  - "Transcribe skipped clips" on a talkgroup.
  - Both give every row the same `updated_at`.
- **Effect:**
  - Once more than 500 rows share one timestamp, every poll returns those same 500 rows. All
    of them were already sent, so the cursor never moves, and newer rows are never fetched.
  - Open pages and phones that reconnect with the last event id stay frozen. They recover
    only as the worker re-transcribes the batch, which can take hours.
  - Reloading the page fixes it.
- **Verified:** a test with 600 rows updated at once. The first poll returned 500 rows. Every
  later poll returned 0, including a new transmission that arrived afterwards.
- **Fix:** page the overlap window by `(updated_at, id)` instead of skipping rows already
  sent.

### H3. "Restart worker" during a crash backoff leaves two workers running
When the worker dies, `WorkerSupervisor.watch()` waits out a backoff of 1–60 s, then starts a
new worker. It doesn't check whether one was started in the meantime.

- **Trigger:** Admin → "Restart worker" during that wait. This is likely when the worker keeps
  crashing, which is exactly when you'd click it.
- **Effect:**
  - `restart()` starts a worker, then `watch()` starts another and replaces the reference.
  - The first worker is orphaned. It keeps claiming clips and holding the GPU, and it isn't
    stopped when `run` shuts down.
- **Verified:** a test with fake processes: 3 were started and 2 were alive at the end.
- **Fix:**
  - Put one lock around start, stop and restart.
  - After the backoff, `watch()` restarts only if the process it saw die is still the
    current one.
  - Reset the crash counter after a period of healthy running (see M4).

## Medium

### M1. The nightly VACUUM holds the write lock for about as long as writers wait
The write timeout is `busy_timeout` = 5 s.

- **Measured:** VACUUM of the year database after deleting a quarter of its rows took
  **4.1 s** (about 0.6 GB after compaction).
- **Effect:**
  - A slower disk or more data exceeds the 5 s, and then capture fails (H1) or the worker
    crashes (M4).
  - A test writer during the VACUUM got through after 2 s this time.
- **Fix, either:**
  - **(a)** Drop the automatic VACUUM. Once retention reaches steady state, SQLite reuses
    the freed pages, so the file stops growing. Keep `retention --vacuum` for manual use.
  - **(b)** Raise the writers' `busy_timeout` to 30 s.
- **Recommendation:** both, plus H1.

### M2. Interrupted prompt tests stay "running" forever
- **Trigger, either:**
  - The worker restarts during a test.
  - The command expires unclaimed after 10 min, because no worker was running.
- **Effect:**
  - The `prompt_evals` row stays `running` or `queued`.
  - It can't be deleted, because delete refuses running tests.
  - Retention skips it.
  - The Admin page keeps polling for it indefinitely.
- **Verified:** by reading the code. Nothing ever moves a test out of `running` except
  finishing or a caught error, and the worker's startup doesn't check for leftover tests.
- **Fix:** on worker start, mark leftover `running` or `queued` tests as failed ("worker
  restarted"). Do the same when their command expires.

### M3. A backslash in a substitution breaks every transcription
`guards.substitute` passes the replacement text to `re.sub` as a template.

- **Trigger:** saving a replacement such as `10\4` on the Admin page.
- **Effect:**
  - `re.sub` raises "invalid group reference" on every clip.
  - Every clip fails, and after the retry limit it is marked failed.
- **Verified:** `substitute("unit ten four", {"ten four": r"10\4"})` raises `re.error`.
- **Fix:** pass the replacement as a function, so it's used literally. Also validate on save.

### M4. A database error in the worker's housekeeping crashes the worker
Only the transcription of a clip is protected. The heartbeat, claim, recovery, version check
and command polling are not.

- **Trigger:** a lock held for more than 5 s.
- **Effect:**
  - The worker exits. The supervisor restarts it, which reloads the models.
  - The supervisor's crash counter never resets. After 7 crashes over the system's lifetime,
    every restart waits the full 60 s.
- **Fix:**
  - Catch and log errors in the loop body, then sleep briefly.
  - Reset the counter after about 10 minutes of healthy running.

### M5. Pages that get slow with a year of data
Measured on the synthetic year:

| Page / query | Time | Cause | Fix |
|---|---|---|---|
| Search page (any query) | 2.2–3.1 s | 1.2 s is the talkgroup chips (`recent_talkgroups` over all history); the rest is results plus count (0.2–0.7 s each) | Build the chips from the `channels` table (it has `last_heard_at`). The Admin re-transcribe section has the same chips. |
| Transmission detail page | 0.6 s | "Newer on this talkgroup" uses an `OR` keyset, so SQLite sorts every row after the cursor. It gets slower the older the clip. | Row-value keyset `(started_at, id) > (?, ?)`, plus an index on `(tgid, started_at)` |
| Live feed filtered to a talkgroup (`/?tg=`) | 0.5–0.7 s | No talkgroup index | The same `(tgid, started_at)` index |
| `/status` and `/api/status` | 0.8 s | `jobs.stats` filters on `transcribed_at`, which has no index (0.35 s, run twice) | Add an index, or filter on `updated_at` (indexed) first |
| Admin page | about 0.26 s per saved prompt version | The "used by N clips" count does a full scan for each version | Index `prompt_version` and `subs_version`, or drop the counts |
| Every live page | 0.12 s | `MIN(started_at), MAX(started_at)` in one query scans the whole index | Two separate subqueries (instant) |

These are fine: the live feed's first page (0.19 s), "Load older" (5 ms), the JSON API, and
the health check that every page and every open tab runs every 5 s (70 ms). A retention pass
over the year took 0.7 s as a dry run.

## Low

- **L1. A screenshot showing transcripts is committed** (`web-feedback.png`). The repo has no
  remote, so this is hygiene only. Fix: `git rm` it and add `*.png` to `.gitignore`.
- **L2. Login rate limiting is per IP address.** Through the Tailscale subnet router, all
  remote clients may share one IP, so someone's typos can delay someone else's login by up
  to 60 s. Acceptable for a household; my recommendation is won't fix.
- **L3. Plain HTTP on the LAN. Decided (Dan):** the web server listens on `127.0.0.1` over
  HTTP, and `tailscale serve` provides HTTPS. `[web] bind` accepts `127.0.0.1`, one address,
  or `*` for every interface; `*` was added in the review, with a test. Behind the proxy,
  uvicorn's proxy headers are trusted from 127.0.0.1. If Tailscale sends `X-Forwarded-*`
  headers, the cookie becomes `Secure` and login rate limiting sees real client IPs (check
  this in Phase C).
- **L4. The live source retries only on network errors.** An unexpected exception, such as
  a library bug or odd XML, ends capture. TEARDOWN is still sent. Fix: log any other
  exception and retry with backoff (together with H1).
- **L5. Prompt tests in degraded mode score the CPU fallback model** without saying so, so
  the WER is misleading. Fix: refuse to run, or label the result "degraded".
- **L6. A paused live page buffers new rows with no limit.** A tab left paused for days
  keeps growing. Fix: cap the buffer and show "many new — reload".
- **L7. `/events?since=<long ago>` re-sends history** in 500-row batches to anyone on the
  LAN. My recommendation is won't fix: it's read-only, and the same data is public on the
  site anyway.

## Maintainability and docs

- **D1. The README is behind the code.**
  - It doesn't list the `retention` command.
  - It still says "M3 apart from one pending test".
  - It has no operations section.
  - Fix in Phase C along with the service docs.
- **D2. The size estimate in `m5-plan.md` is about 7× too low.** It assumed 50–100k rows a
  year; the measured traffic gives about 0.5–0.7 million. The database projection becomes
  about 0.8 GB, rather than the earlier estimates of a few hundred MB. The audio projection
  (90 days) is unaffected. Update the plan's figures.
- **D3. `web/server.py` is 880 lines inside one `create_app` closure.** It works, and the
  tests cover it well. Splitting it into routers would help later. My recommendation is
  won't fix now.

## Test gaps
Every finding above lacks a test. Beyond those:
- No test simulates a locked database or a full disk for capture or the worker.
- No test runs a VACUUM while capture is writing.
- The browser checks in `tests/ui` run by hand, outside pytest. That's fine as long as the
  Phase B exit checklist runs them.

## Checked and fine
- **RTSP TEARDOWN:** runs on every exit path I traced, including errors inside callbacks,
  close during backoff, and SIGTERM. The flock prevents a second owner.
- **SQL:** every f-string only inserts constant column names or `?` placeholders.
- **XSS:** transcripts and scanner strings are always escaped. The only `Markup` is search
  highlighting, which escapes first. The JavaScript `innerHTML` only receives HTML the
  server escaped.
- **Audio downloads:** the audio path is confined to the data directory.
- **Log viewer:** the component name is checked against an allow-list.
- **Admin writes:** need the session cookie (SameSite=Strict) plus the `HX-Request` header.
  The session secret rotates when the password changes.
- **Queue:** claiming a clip is atomic (`UPDATE … RETURNING`). Stale and dead-worker claims
  are recovered. Retention skips clips that are queued or being processed.
- **Migrations:** each runs in a transaction, and the web server applies them at startup.
- **Full suite:** 227 pytest tests passing before any fixes.

## Triage and outcome (Dan: "go with your suggested triage")

**Fixed, each with a regression test:**

| Finding | Fix | Test |
|---|---|---|
| H1 | A clip save is retried after 0.5, 2 and 5 s. If it still fails, the WAV goes to `data/spill/`, a `clip_not_saved` event is recorded, and capture carries on. The "capture stopped" write can no longer hide the real reason for stopping. Writers wait 15 s for the lock instead of 5 s. | `test_replay_app.py`: retried save; spilled audio |
| H2 | The change feed pages by `(updated_at, id)` keyset. Late commits are found with a cheap `(id, updated_at)` scan of the window, capped at 500 rows per poll. | `test_changes.py`: 1,200 rows with one timestamp; 700 late commits |
| H3 | The supervisor's start, stop and restart share one lock, and `watch()` replaces only the process it saw die. | `test_supervisor.py` (4 tests) |
| M1 | VACUUM only runs when asked, with `retention --vacuum`. The `vacuum_hour` and `vacuum_free_ratio` settings are removed. Writers wait 15 s (see H1). | `test_retention.py::test_vacuum_only_on_request` |
| M2 | At start, the worker marks tests left `running` as failed ("interrupted: the worker restarted") and closes their commands. Queued tests whose command expired are marked failed, both by the worker and by `run`'s command poll. | `test_prompteval.py` (2 tests) |
| M3 | Substitution replacements are literal text (a function replacement). | `test_guards.py` |
| M4 | The worker loop catches and logs unexpected errors, pauses 2 s and goes on, and its shutdown writes are guarded. The crash backoff resets after 10 minutes of healthy running. | `test_worker.py`; `test_supervisor.py` |
| M5 | Migration 0007 adds indexes on `(tgid, started_at)`, `transcribed_at`, `prompt_version` and `subs_version`. Paging (feed and search) uses row values, the date bounds use two subqueries, and `jobs.stats` uses `+status` to stay on the `transcribed_at` index. Search and Admin talkgroup chips come from `channels`, via `feed.known_talkgroups`: busiest in 7 days first. | `test_feed.py`: ties in both directions; `known_talkgroups` |
| L1 | `web-feedback.png` and a stray `.config.toml.swp` are removed from git, and `*.png` and `*.swp` are ignored. The files stay on disk. | — |
| L4 | The live source retries after any exception, with backoff and TEARDOWN, instead of ending capture. | `test_live_source.py` |
| L3 | Decided: listen on 127.0.0.1 and use `tailscale serve` for HTTPS. `bind = "*"` means every interface. | `test_web.py` |

**Performance on the synthetic year, after the fixes:**

| Page | Before | After |
|---|---|---|
| Search | 2.2–3.1 s | 0.5–1.3 s |
| Detail page | 0.64 s | 0.07 s |
| Live feed with `?tg=` | 0.70 s | 0.15 s |
| Live feed | 0.19 s | 0.15 s |
| `/status`, `/api/status` | 0.83 s | 0.13 s |

What remains in search is the FTS matching itself. The synthetic data uses only 20 distinct
words, so every term matches 15–20% of all rows, which is a worst case.

**Also fixed while verifying:** the `tests/ui/channels_check.py` browser check predated the
M4.5 login requirement. It now logs in first, and it also checks that a logged-out visitor
sees no edit boxes. The test helper `add()` now creates the `channels` row, as production
does.

**Exit check:**
- pytest: 243 passed.
- All six `tests/ui` browser checks pass: live updates, reconnect, row cap, channels, admin
  and player.

**Later:**
- L5 (label degraded prompt tests).
- L6 (cap the paused-page buffer).
- D1 (the README, with Phase C).
- D2 (the size figures in the plan).

**Won't fix:** L2, L7, D3.
