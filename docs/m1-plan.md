# M1 plan: capture, segmentation and storage

**Status (2026-09-30): done.** All steps are implemented and every exit check passed.
Live run: 31 clips in 10 minutes, 0 packets lost. Start/stop: 20/20 cycles released the audio
session. Held-out listening review: 2 of 30 clips were flagged. One (0101, two units merged)
was fixed by the unit-handoff split. The other (0113, first word cut) was the scanner's own
late unmute.

**Goal:** `python -m scanner_transcriber run` connects to the scanner (or replays a
recording), cuts the live stream into transmissions, saves each clip, and inserts a DB row with
`status='queued'`, ready for the M2 transcription worker. There is no STT or web UI in M1.

**Exit criteria**

- Replaying `data/sessions/20260929T233522Z` produces about 92 clips. The within-PTT splits
  (0073/0074, 0078/0079) come out as single clips, and every clip has a TGID and channel key.
- 10 minutes live against the scanner: clip boundaries sound right, and nothing is dropped
  when two talkgroups follow each other closely.
- 20 start/stop cycles, including Ctrl-C and `kill`, leave port 554 usable (design R2).
- Unit tests for the segmenter, DB and replay pass using synthetic fixtures only.

## What M0 changed about the design

| Design doc (§5.3) | M1 decision | Evidence |
|---|---|---|
| Segment on squelch with 1.5–2 s **hangtime**; same channel reopening = same clip | **One clip per PTT**: a clip is one continuous `Unmute` span on one TGID. A short **debounce** (about 400 ms, two PSI intervals) absorbs jitter. Back-and-forth exchanges become consecutive rows, not one clip. | 92 status spans ≈ 92 PTTs. 29 of 38 same-TG gaps under 3 s were a *different unit* answering. |
| Audio energy / VAD for boundaries | Status gives boundaries. Audio only **trims** leading and trailing exact-zero runs. It also serves as a fallback (non-zero runs) when PSI is stale. No level gate and no VAD dependency. | Exact zeros and a ±8 floor also occur inside calls, and quiet clips were real speech. |
| `audio_latency_ms` to tune | Default 0, kept configurable. | Offset is within the 250 ms PSI interval. |
| Channel key TBD | `sys:<system>\|tg:<tgid>` | TGID is present on every voice PSI. |

## Decisions (agreed 2026-09-30)

- **One row per key-up (PTT).** Exchanges are grouped visually in the UI later, not merged
  into one clip.
- **Clip audio is 16-bit PCM WAV.** That's about 16 KB/s, roughly 100 MB/day at current
  traffic, and it plays in every browser with no encoder.
- **Ignoring talkgroups:**
  - Hard ignore uses **Avoid on the SDS200 itself** (front panel or Sentinel). The scanner
    never stops on those talkgroups, so it stays free for others.
  - The app's own `ignored` flag (M4) is a soft ignore: *record, don't transcribe*.
  - The `sds200` library has no remote Avoid command, and v1 doesn't control the scanner
    anyway.

## Components

- **`models.py`**
  - `ScannerStatus`, built from the existing `summarize_status` fields, plus raw XML.
  - `AudioFrame`: sample index on the RTP timeline, int16 PCM, receive time.
  - `Clip`: start/end sample, PCM, starting status, unit IDs, status changes, `continued` flag.
- **Timebase.** Everything downstream works in **sample time**, the RTP-derived position in
  the 8 kHz stream. It's immune to network jitter and identical between live and replay.
  Status events are mapped into sample time with the monotonic anchor method `record` already
  uses, minus `audio_latency_ms`.
- **`source/base.py`**
  - `ScannerSource` protocol from design §5.1: `status_events()`, `audio_frames()` and
    `close()`, all async.
- **`source/sds200_network.py`**: `SDS200NetworkSource`.
  - Wraps `SDSScanner` (PSI push) and `NetworkAudioTransport`.
  - Bridges the library's callback threads into asyncio queues with `call_soon_threadsafe`.
  - Moves the recorder's RTP-timestamp → sample-index logic (`_WavTimeline`) into a shared
    `RtpTimeline`.
  - Liveness: no RTP for 5 s, or no PSI for 5 s, triggers a reconnect with exponential backoff
    capped at 60 s.
  - Always sends TEARDOWN (in `close()`, on signals, and at exit) and holds `AudioSessionLock`.
- **`source/replay.py`**: `ReplaySource`.
  - Reads a `record` session directory and yields frames and statuses in sample-time order.
  - `--speed realtime|max`.
  - `record` keeps its current output format, so every session recorded so far stays
    replayable.
- **`segmenter.py`**: a pure, synchronous state machine, `feed_status()` / `feed_audio()` →
  finished `Clip`s. The asyncio wrapper is a thin loop around it, so tests need no event loop
  or hardware.
  - **IDLE → OPEN** on `Unmute` with a TGID. The 300 ms pre-roll ring buffer is included.
  - **OPEN → CLOSING** on `Mute`.
  - **CLOSING → OPEN** if the same TGID unmutes within the debounce window (still one PTT).
  - **CLOSING → emit** once the debounce expires.
  - **TGID change while open:** emit the current clip and open a new one immediately.
  - **Same-unit re-keys stay separate** (Dan, 2026-09-30, after reviewing TG 1201 at
    14:45): dispatchers often release the channel between sentences, and the radio system
    reports each as its own transmission (the unit ID clears, then 1–3 s of silence). Each
    key-up remains its own row, shown together in the same exchange block. Merging them for
    transcription context was considered and left for possible later experiment.
  - **Unit ID change while unmuted:** the scanner can stay unmuted from one unit's PTT
    straight into another unit's reply (found in held-out review, clip 0101). Split at the
    last silence of at least 200 ms before the change. A stale unit at clip start doesn't
    cause a split.
  - **Longer than `max_clip_s` (60):** emit, then continue with `continued=True`.
  - **On emit:** trim leading and trailing exact zeros, keep about 100 ms of padding, and drop
    the clip if under `min_clip_ms` (400) of non-zero audio.
  - **Status stale for more than 2 s:** fall back to audio-only runs, labelled with the last
    known TGID and flagged `status_missing`.
- **`clipstore.py`**: `data/clips/YYYY/MM/DD/HHMMSS_<id>.wav` in **local time** (changed
  2026-09-30 so clips can be found by hand; earlier clips keep their `<id>.wav` paths,
  which are stored in each row). 16-bit PCM 8 kHz mono. Retention (M5) should select by
  the row's `started_at` and delete the stored path, not go by folder names.
- **`db.py` + `migrations/0001_init.sql`**
  - The design §6 schema: `channels`, `transmissions`, FTS5 table plus sync triggers, and
    `schema_version`.
  - WAL mode; migrations applied at startup.
  - Thin repository functions: `upsert_channel`, `insert_transmission` (queued), `recent`.
  - Plain `sqlite3`, since the web layer (M3) is the only async consumer.
- **`app.py` + `run` command**
  - Pipeline: source → segmenter → clip store + DB.
  - `run --replay <session> [--speed max]` for development.
  - Graceful shutdown on SIGINT/SIGTERM flushes any open clip.
  - Adds a `[segmenter]` config section (`debounce_ms`, `preroll_ms`, `pad_ms`, `min_clip_ms`,
    `max_clip_s`, `audio_latency_ms`, `status_stale_ms`).

## Tests

- **Segmenter, with synthetic event/audio fixtures built in code:**
  - a single PTT;
  - PSI jitter inside one PTT (Mute/Unmute flicker shorter than the debounce);
  - a new PTT from another unit after 1 s → two clips;
  - a TGID change while unmuted;
  - a 90 s transmission split into two with the `continued` flag;
  - a short click dropped;
  - pre-roll present;
  - leading and trailing zeros trimmed;
  - stale status → audio fallback.
- **DB:** migrations apply to an empty DB; FTS triggers track insert/update/delete; channel
  upsert updates `last_heard_at`.
- **Integration:** `ReplaySource(max)` on a synthetic session → expected rows and files.
- **Local-only regression (not committed):** replay the real 50-minute session and print a
  clip-count and boundary report against the status spans.
- **Hardware (manual):** a 10-minute live run and the 20× start/stop teardown check.

**Privacy:** real recordings stay in `data/` (gitignored). They contain names, addresses and
licence details, so committed fixtures are synthetic only.

## Order of work

1. `models.py`, `RtpTimeline` extracted from `record` (with tests), and config additions.
2. Segmenter plus its unit tests. This is the core, and it's pure logic.
3. `ReplaySource`, then the local regression on the real session. Tune the debounce.
4. Migrations, `db.py`, clip store, and their tests.
5. `app.py` / `run --replay` end to end.
6. `SDS200NetworkSource` with reconnect; live 10-minute run; teardown cycles. Needs the
   scanner free (no `record` running).

## Out of scope (later milestones)

STT worker and queue claiming (M2), web UI and SSE (M3), search UI and channel editing (M4),
systemd, retention and soak test (M5).
