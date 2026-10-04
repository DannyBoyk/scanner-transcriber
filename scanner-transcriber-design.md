# Scanner Transcriber — Design Document

**Status:** Draft v0.1 (2026-09-29). This is the original plan; later decisions override it
where they differ. See `docs/m0-results.md` (STT choice, speed target), `docs/m1-plan.md`
(per-PTT segmentation, storage) and `docs/m2-plan.md` (single worker, guards). The README has
the current status.
**Owner:** Dan
**Target host:** Intel NUC 11 (Celeron N5105, 4C/4T, 8 GB DDR4, 256 GB SSD), Linux
**Radio:** Uniden SDS200, connected over Ethernet

---

## 1. Purpose

Build a small, self-hosted service that:

1. Connects to an SDS200 scanner over the LAN.
2. Captures its audio stream and channel metadata.
3. Splits the continuous audio into individual transmissions.
4. Transcribes each transmission locally with a small speech-to-text (STT) model.
5. Catalogues transmissions (time, system, department, channel/talkgroup, frequency, transcript) in a database.
6. Serves a live-updating, searchable web page, reachable only over the Tailscale tailnet.

This is essentially a lightweight, text-first ProScan-style logger. No cloud services are involved.

### 1.1 Goals

- Runs unattended 24/7 on the NUC with no manual intervention.
- Keeps up with typical scanner traffic, with transcripts appearing within tens of seconds of a transmission ending.
- Supports search by transcript text, channel/talkgroup, system, department, and time range.
- Shows new transmissions on the live page without a manual refresh.
- Keeps the footprint modest: a single Python service, SQLite, and no JVM, container orchestrator, or external database.

### 1.2 Non-goals (for v1)

- Controlling the scanner (tuning, holding, changing favorites) from the web UI. Scanner control is read-only in v1, apart from the commands needed to receive status.
- Public internet exposure.
- Multi-scanner support. Keep the design scanner-ID-aware so it can be added later.
- Speaker diarization, translation, or LLM summarization.
- Decoding encrypted traffic, which is out of scope and not possible anyway.

---

## 2. Key constraints and risks

| # | Constraint / risk | Impact | Mitigation |
|---|---|---|---|
| R1 | The N5105 (Tremont cores) is believed to lack AVX/AVX2. Most STT runtimes are much slower without it. | STT throughput is the main feasibility risk. | **Milestone 0 is a benchmark on the actual NUC** before any other work. The STT backend is pluggable. The iGPU via OpenVINO is an optional path. |
| R2 | The SDS200 allows only **one** RTSP audio session. A leaked session can lock out port 554 until the scanner is power-cycled. | The service must be the sole audio owner and must always tear down cleanly. | Always send RTSP TEARDOWN (on signals, exceptions, shutdown). Enforce a single owner. Never probe 554 casually. |
| R3 | Audio is 8 kHz G.711 µ-law (narrowband), while models expect 16 kHz. | Accuracy is capped. Radio jargon, unit IDs, and street names cause errors. | Resample to 16 kHz. Use a vocabulary/prompt bias where supported. Keep the audio so a person can verify. |
| R4 | The audio stream is continuous. The scanner does not deliver discrete "calls". | We must segment transmissions ourselves. | A metadata-driven segmenter with an energy/VAD backstop (§5.3). |
| R5 | Metadata (UDP) and audio (RTSP/RTP) arrive on separate paths with different latency. | Clips can be mislabeled at channel boundaries. | Timestamp both sources and apply a configurable audio-latency offset (§5.3). |
| R6 | Sustained CPU load on a small-form-factor box. | Thermal throttling reduces STT throughput. | Monitor CPU clock and temperature. Size the worker pool. Keep a backlog policy (§5.4). |
| R7 | Whisper-family models hallucinate on silence or noise. | Garbage transcripts. | Trim silence, drop very short clips, apply a no-speech threshold, and flag low-confidence output. |

---

## 3. Technology choices

| Concern | Choice | Rationale |
|---|---|---|
| Language | **Python 3.12** (3.11+ minimum) | The SDS200 client library and all STT runtimes have first-class Python bindings. The code is mostly I/O plus native inference, so Python speed isn't the bottleneck. |
| Concurrency | `asyncio` for capture and web. **Separate worker process(es)** for STT. | Inference is CPU-bound and must never block audio capture or the web server. |
| Scanner I/O | The [`sds200`](https://pypi.org/project/sds200/) PyPI package (control/PSI over UDP 50536, audio over RTSP/RTP on TCP 554), wrapped behind our own interface | It's hardware-validated against SDS200 firmware 1.26.01 and handles the nonstandard RTP port negotiation. **Check its license and API before depending on it.** If it doesn't fit, implement the protocol directly using the Uniden *SDS Series Remote Command Specification* as reference. |
| STT | Pluggable backend. Candidates, in benchmark order: **Moonshine** (ONNX), **Parakeet 110M/0.6B** (ONNX or parakeet.cpp), **whisper.cpp** tiny.en/base.en (optionally with the OpenVINO encoder) | Chosen by Milestone 0 results. |
| Web framework | **FastAPI** + **Uvicorn** | Lightweight, async, easy SSE support. |
| Templating / UI | **Jinja2** server-rendered pages + **htmx** (+ its SSE extension). No SPA build step. | Minimal front-end tooling. Pages stay mostly static HTML. |
| Live updates | **Server-Sent Events** (`sse-starlette` or a hand-rolled `StreamingResponse`) | One-way feed, auto-reconnect, simpler than WebSockets. |
| Database | **SQLite** (WAL mode) + **FTS5** full-text index | Zero-ops, fast enough for this volume, built-in full-text search. |
| Audio encoding | Store clips as **Opus** (via `ffmpeg` or `pyogg`) or keep µ-law WAV | Opus at ~8–12 kbps is tiny. µ-law WAV avoids an encoder dependency. Decide in M1. |
| Config | A single **TOML** file (`tomllib`) + environment overrides | Simple, typed with pydantic-settings or dataclasses. |
| Packaging | `uv` (or pip) + `pyproject.toml` | Reproducible installs on the NUC. |
| Process supervision | **systemd** unit | Restart on failure. Journal logging. |
| Exposure | **`tailscale serve`** → local Uvicorn port | Tailnet-only HTTPS with no nginx needed. |

---

## 4. Architecture overview

```
                 ┌────────────────────────── NUC (single systemd service) ─────────────────────────┐
                 │                                                                                 │
 SDS200          │  ┌──────────────┐   status events   ┌─────────────┐   clip + metadata          │
 UDP 50536 ◄────►│  │ ScannerSource│──────────────────►│  Segmenter  │───────────┐                │
 (control/PSI)   │  │  (asyncio)   │   PCM frames      │ (state mach)│           ▼                │
 TCP 554 RTSP ──►│  │              │──────────────────►│             │   ┌──────────────┐         │
 RTP PCMU 8 kHz  │  └──────────────┘                   └─────────────┘   │  Clip store  │ (disk)  │
                 │                                                       │  + DB row    │         │
                 │                                                       │ status=queued│         │
                 │                                                       └──────┬───────┘         │
                 │                                                              │                 │
                 │                                   ┌──────────────────────────▼──────────┐      │
                 │                                   │ Transcription worker(s) (process)   │      │
                 │                                   │  resample 8k→16k → STT backend      │      │
                 │                                   │  writes transcript, status=done     │      │
                 │                                   └──────────────────────────┬──────────┘      │
                 │                                                              │ notify           │
                 │  ┌────────────────────────────────────────────────────────── ▼ ─────────────┐   │
                 │  │ Web app (FastAPI): pages, search API, SSE /events, audio playback       │   │
                 │  └─────────────────────────────────────────────────────────────────────────┘   │
                 │                     ▲                                                          │
                 └─────────────────────┼──────────────────────────────────────────────────────────┘
                                       │ tailscale serve (HTTPS, tailnet only)
                                  Browsers on tailnet
```

**Process model:** one main process runs the asyncio loop (ScannerSource, Segmenter, Web app, retention job). Transcription runs in 1–N worker processes. The **SQLite table is the durable queue**, so clips survive restarts and a crash in a worker never loses audio.

---

## 5. Components

### 5.1 `ScannerSource` (interface + implementations)

Abstracts where status and audio come from, so everything downstream is testable without hardware.

```python
class ScannerSource(Protocol):
    async def status_events(self) -> AsyncIterator[ScannerStatus]: ...
    async def audio_frames(self) -> AsyncIterator[AudioFrame]: ...   # 8 kHz mono int16 PCM + monotonic timestamp
    async def close(self) -> None: ...                               # MUST send RTSP TEARDOWN
```

Implementations:

1. **`SDS200NetworkSource`** is the real scanner.
   - Control/metadata: UDP to `<scanner_ip>:50536`. Use the scanner's pushed status stream (PSI) where available, and fall back to polling `GSI` at a configurable interval (e.g., 250–500 ms).
   - Audio: RTSP `rtsp://<ip>/au:scanner.au` → RTP PCMU 8 kHz mono (≈40 ms packets). Send a `GET_PARAMETER` keepalive about every 15 s. Decode µ-law to int16.
   - **Must** register TEARDOWN on SIGTERM/SIGINT, on unhandled exceptions, and in `close()`.
   - Reconnect with exponential backoff (cap ~60 s) if status goes stale or RTP stops. Log packet loss, late packets, and timestamp discontinuities.
2. **`ReplaySource`** replays a recorded session from files (a WAV plus a JSONL of timestamped status events). It is used for development, tests, and benchmarks. **Build this early** so most work can happen away from the scanner.
3. **`RecorderTool`** (CLI) records a live session from the real scanner into ReplaySource format. It is needed for Milestone 0.

`ScannerStatus` (normalized from GSI/PSI XML — **verify exact field names against real scanner output**):

```python
@dataclass(frozen=True)
class ScannerStatus:
    ts: float                 # monotonic receive time
    wall_ts: datetime         # UTC
    mode: str                 # e.g. scan / hold / id search (raw value kept)
    system: str | None
    department: str | None
    channel: str | None       # alpha tag / channel name
    tgid: str | None          # talkgroup ID for trunked systems
    unit_id: str | None       # if reported
    frequency_hz: int | None
    modulation: str | None    # FM/NFM/P25/DMR etc. if reported
    squelch_open: bool        # derived from status (signal/mute indicators) — verify
    rssi: int | None
    raw: dict                 # full parsed payload, kept for debugging
```

### 5.2 Timebase

All events and audio frames carry a **monotonic** timestamp taken on receipt. Wall-clock UTC is stored for display. A config value `audio_latency_ms` (default 0, to be tuned) is subtracted from audio timestamps to align them with metadata, since RTP buffering usually delays audio slightly.

### 5.3 `Segmenter`

Turns the continuous audio stream plus status updates into discrete **transmission clips**.

**State machine:**

```
IDLE ──(squelch opens OR channel/tgid becomes active)──► ACTIVE
ACTIVE ──(squelch closes)──► HANGTIME
HANGTIME ──(same channel reopens within hangtime_ms)──► ACTIVE   (same clip)
HANGTIME ──(hangtime_ms elapses)──► FINALIZE ──► IDLE
ACTIVE ──(channel/tgid changes)──► FINALIZE current, start new ACTIVE
ACTIVE ──(duration > max_clip_s)──► FINALIZE, start new ACTIVE (same channel, flagged "continued")
```

Rules:

- Keep a **pre-roll ring buffer** (e.g., 300 ms) so the start of speech isn't clipped.
- **Energy/VAD backstop:** if status is unavailable or ambiguous, fall back to audio energy thresholds to detect speech boundaries. Use a lightweight VAD (e.g., `webrtcvad` or Silero VAD ONNX, whichever is cheaper on this CPU) to trim leading and trailing silence.
- Drop clips shorter than `min_clip_ms` (e.g., 400 ms) or with no detected speech. Log them as counts, not rows, or store them with `status='skipped'` if configured.
- Attach the metadata snapshot from the **start** of the clip, plus a list of any status changes during it.
- On finalize: write the audio to the clip store, insert a DB row with `status='queued'`, and notify workers.

Tunables (in config): `hangtime_ms` (default 1500–2000), `min_clip_ms`, `max_clip_s` (default 60), `preroll_ms`, `vad_aggressiveness`, `audio_latency_ms`.

### 5.4 Transcription workers

- Run as separate process(es) (`multiprocessing`, spawn start method). They poll or claim work from SQLite with an atomic claim: `UPDATE ... SET status='processing', claimed_at=? WHERE id = (SELECT id FROM transmissions WHERE status='queued' ORDER BY priority DESC, started_at ASC LIMIT 1) RETURNING id`.
- Pipeline per clip:
  1. Load audio.
  2. Resample 8 kHz → 16 kHz (`soxr` or `scipy.signal.resample_poly`).
  3. Optionally apply a band-pass filter (300–3400 Hz) and normalization.
  4. Run the STT backend.
  5. Post-process: apply the vocabulary substitution map, collapse repeated n-grams (a hallucination guard), and flag empty or low-confidence output.
  6. Write the transcript, confidence, model name/version, and processing time. Set `status='done'` (or `'failed'` with an error message, retried up to N times).
- **Backlog policy:** if queue depth exceeds `backlog_high_water`, workers process by `priority` (configurable per system/channel/department) and may skip channels marked `low` until the queue drains. Surface queue depth on the status page.
- **Worker count:** default 1. Tune after Milestone 0. The 4 cores may be better used as 1 worker × 3–4 threads than as 4 single-thread workers.

**STT backend interface:**

```python
class SttBackend(Protocol):
    name: str
    def load(self, cfg: SttConfig) -> None: ...
    def transcribe(self, pcm16k: np.ndarray) -> SttResult: ...

@dataclass
class SttResult:
    text: str
    confidence: float | None      # backend-specific; normalize to 0–1 when possible
    segments: list[dict] | None   # word/segment timings if available
    no_speech: bool
```

Implementations are selected by config: `moonshine`, `parakeet`, `whispercpp`. Where the backend supports an initial prompt or hotword biasing (Whisper does), feed it a short vocabulary string from config (unit names, local streets, agency names).

### 5.5 Clip store

- Path layout: `data/clips/YYYY/MM/DD/<transmission_id>.<ext>`.
- Format decided in M1: Opus (smaller, needs an encoder) vs µ-law WAV (≈8 KB/s, no encoder). At 8 KB/s, even 10 hours/day of traffic is ~290 MB/day raw, and Opus is roughly a quarter of that. Retention keeps this bounded.
- The web app serves clips with HTTP range support for scrubbing.

### 5.6 Retention job

An asyncio task that runs periodically (e.g., hourly):

- Delete audio older than `retention.audio_days` (default 14). Set `audio_path = NULL` but keep the row and transcript.
- Optionally delete rows older than `retention.transcript_days` (default: keep forever).
- Run `PRAGMA wal_checkpoint(TRUNCATE)` and an occasional `PRAGMA optimize`.

### 5.7 Web application

**Pages** (server-rendered, htmx-enhanced):

- `/`: **Live feed.** Newest transmissions at the top. Each row shows the time, system / department / channel (or TGID), a transcript (or a "transcribing…" placeholder), confidence styling, and a play button. New rows arrive through SSE and are prepended. Includes a pause-autoscroll toggle and optional channel filter chips.
- `/search`: **Search.** Full-text query (FTS5 syntax with a simple mode), plus filters for system, department, channel/TGID, date range, minimum confidence, and "has audio". Results are paginated and highlighted with FTS5 `snippet()`.
- `/t/{id}`: **Transmission detail.** Audio player, full transcript, metadata, raw status JSON (collapsible), and model/timing info.
- `/channels`: A list of channels/talkgroups seen, with counts and a last-heard time. Clicking one goes to a filtered search. Allows setting priority, display alias, and ignore flags.
- `/status`: Health page showing scanner connection state, last status and audio timestamps, RTP loss counters, queue depth, worker throughput (real-time factor), CPU temperature/frequency, and disk usage.

**API** (JSON, used by htmx fragments and future clients):

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/transmissions` | List with filters: `q`, `system`, `department`, `channel`, `tgid`, `from`, `to`, `min_conf`, `limit`, `before_id` (keyset pagination) |
| GET | `/api/transmissions/{id}` | Detail |
| GET | `/api/transmissions/{id}/audio` | Audio (range requests) |
| GET | `/api/channels` | Channel list + stats |
| PATCH | `/api/channels/{key}` | Update alias/priority/ignore |
| GET | `/api/status` | Health JSON |
| GET | `/events` | SSE stream: `transmission.created`, `transmission.transcribed`, `status.changed` |
| GET | `/healthz` | Liveness for systemd/monitoring |

**SSE notes:** workers run in other processes, so they signal the web process via a small mechanism. Options: a `multiprocessing.Queue`, a Unix-socket ping, or the web process polling `SELECT ... WHERE updated_at > last_seen` every ~500 ms. **Start with polling**, which is simplest and robust, and optimize later if needed. Include an `id:` in each SSE event so a reconnecting browser can resume with `Last-Event-ID`.

**Auth:** none in v1. Access control is the tailnet (Tailscale ACLs). Bind Uvicorn to `127.0.0.1` only, and let `tailscale serve` front it.

---

## 6. Data model (SQLite)

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE channels (
    key            TEXT PRIMARY KEY,          -- stable key, e.g. "sys:<system>|tg:<tgid>" or "sys:<system>|ch:<channel>|f:<freq>"
    system         TEXT,
    department     TEXT,
    channel        TEXT,
    tgid           TEXT,
    frequency_hz   INTEGER,
    alias          TEXT,                      -- user-defined display name
    priority       INTEGER NOT NULL DEFAULT 0,-- higher = transcribed first under backlog
    ignored        INTEGER NOT NULL DEFAULT 0,-- 1 = capture but don't transcribe (or don't capture; configurable)
    first_heard_at TEXT NOT NULL,
    last_heard_at  TEXT NOT NULL
);

CREATE TABLE transmissions (
    id              INTEGER PRIMARY KEY,
    scanner_id      TEXT NOT NULL DEFAULT 'sds200-1',
    channel_key     TEXT REFERENCES channels(key),
    started_at      TEXT NOT NULL,            -- UTC ISO-8601
    ended_at        TEXT NOT NULL,
    duration_ms     INTEGER NOT NULL,
    system          TEXT,                     -- denormalized snapshot at start of clip
    department      TEXT,
    channel         TEXT,
    tgid            TEXT,
    unit_id         TEXT,
    frequency_hz    INTEGER,
    modulation      TEXT,
    rssi            INTEGER,
    status_changes  TEXT,                     -- JSON list of status changes during clip
    raw_status      TEXT,                     -- JSON of the starting status payload
    audio_path      TEXT,                     -- NULL after retention purge
    audio_format    TEXT,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','processing','done','failed','skipped')),
    priority        INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    claimed_at      TEXT,
    error           TEXT,
    transcript      TEXT,
    confidence      REAL,
    no_speech       INTEGER,
    stt_backend     TEXT,
    stt_model       TEXT,
    stt_ms          INTEGER,                  -- processing time; RTF = stt_ms / duration_ms
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX ix_tx_started      ON transmissions(started_at DESC);
CREATE INDEX ix_tx_channel_time ON transmissions(channel_key, started_at DESC);
CREATE INDEX ix_tx_queue        ON transmissions(status, priority DESC, started_at);
CREATE INDEX ix_tx_updated      ON transmissions(updated_at);

-- Full-text index over transcript + labels (external-content FTS5 table)
CREATE VIRTUAL TABLE transmissions_fts USING fts5(
    transcript, system, department, channel,
    content='transmissions', content_rowid='id',
    tokenize='porter unicode61'
);
-- Triggers to keep FTS in sync on INSERT/UPDATE/DELETE of transmissions (standard FTS5 external-content pattern).

CREATE TABLE schema_version (version INTEGER NOT NULL);
```

Migrations: keep plain numbered SQL files in `migrations/` and apply them at startup. No ORM needed; use `sqlite3` or `aiosqlite` with thin repository functions.

---

## 7. Configuration (`config.toml`)

```toml
[scanner]
id = "sds200-1"
host = "192.168.1.50"
control_port = 50536
rtsp_port = 554
status_mode = "push"          # "push" (PSI) or "poll" (GSI)
poll_interval_ms = 300

[segmenter]
hangtime_ms = 1800
preroll_ms = 300
min_clip_ms = 400
max_clip_s = 60
audio_latency_ms = 0
vad = "webrtc"                # "webrtc" | "silero" | "energy"
vad_aggressiveness = 2

[stt]
backend = "moonshine"         # "moonshine" | "parakeet" | "whispercpp"
model = "base"                # backend-specific model name/path
threads = 3
workers = 1
prompt = "Medic, Engine, Truck, Battalion, Northfield, Route 12, Quarry Road"   # used if backend supports it
min_confidence_flag = 0.5
max_attempts = 3

[stt.substitutions]           # post-processing, case-insensitive whole-word
"ten four" = "10-4"

[queue]
backlog_high_water = 50       # start priority-only processing above this

[storage]
data_dir = "/var/lib/scanner-transcriber"
audio_format = "opus"         # "opus" | "wav-ulaw"

[retention]
audio_days = 14
transcript_days = 0           # 0 = keep forever
run_every_minutes = 60

[web]
bind = "127.0.0.1"
port = 8080
page_size = 50
```

---

## 8. Repository layout

```
scanner-transcriber/
├── pyproject.toml
├── README.md
├── config.example.toml
├── migrations/
│   └── 0001_init.sql
├── src/scanner_transcriber/
│   ├── __main__.py            # entrypoint: `python -m scanner_transcriber run|record|bench|replay`
│   ├── config.py
│   ├── app.py                 # wires sources, segmenter, workers, web
│   ├── models.py              # dataclasses (ScannerStatus, AudioFrame, Clip, ...)
│   ├── source/
│   │   ├── base.py            # ScannerSource protocol
│   │   ├── sds200_network.py
│   │   └── replay.py
│   ├── segmenter.py
│   ├── audio.py               # µ-law, resampling, VAD, encoding
│   ├── stt/
│   │   ├── base.py
│   │   ├── moonshine.py
│   │   ├── parakeet.py
│   │   └── whispercpp.py
│   ├── worker.py
│   ├── db.py                  # connection, migrations, repositories
│   ├── retention.py
│   ├── bench.py               # Milestone 0 benchmark harness
│   └── web/
│       ├── server.py          # FastAPI app, routes, SSE
│       ├── templates/         # Jinja2 (base, live, search, detail, channels, status)
│       └── static/            # htmx, minimal CSS, small JS for audio player
├── tests/
│   ├── fixtures/              # short recorded sessions (WAV + JSONL status)
│   ├── test_segmenter.py
│   ├── test_db_fts.py
│   ├── test_worker.py
│   └── test_web.py
└── deploy/
    ├── scanner-transcriber.service   # systemd unit
    └── tailscale-serve.md            # notes/commands for exposing via tailscale serve
```

---

## 9. Milestones

Each milestone should end with something runnable and tested.

### M0 — Feasibility benchmark (do first)
- Implement `RecorderTool` using the `sds200` library (or a minimal RTSP client) to record 30–60 minutes of real traffic as WAV + status JSONL.
- Hand-label ~50 clips with reference transcripts. Rough is fine.
- Implement `bench.py`: for each candidate backend/model, report real-time factor (RTF = processing time / audio duration), peak RSS, mean CPU temperature/frequency, and WER/CER against the labels.
- Candidates: Moonshine tiny/base (and small/medium streaming if RTF allows), Parakeet 110M and 0.6B, whisper.cpp tiny.en and base.en (with and without OpenVINO).
- **Exit criterion:** at least one model with RTF ≤ 0.5 on the NUC and usable accuracy. If none qualifies, revisit the hardware decision before continuing.
- First check: `grep -o -w 'avx\|avx2\|sse4_2' /proc/cpuinfo | sort -u` on the NUC to confirm the instruction sets.

### M1 — Capture and segmentation
- `ScannerSource` protocol, `SDS200NetworkSource`, `ReplaySource`.
- Segmenter with unit tests driven by replay fixtures (channel change mid-clip, hangtime reopen, max length split, short-click drop).
- Clip store + DB schema + migrations. Rows are inserted with `status='queued'`.
- Graceful shutdown with a verified RTSP TEARDOWN. Test by starting and stopping the service 20× and confirming port 554 still accepts connections.

### M2 — Transcription pipeline
- STT backend interface + the winning backend from M0 (a second backend is optional).
- Worker process with atomic claim, retries, backlog priority, and post-processing.
- End-to-end with ReplaySource: a replayed session becomes transcribed rows.

### M3 — Web UI (live + detail)
- FastAPI app, base template, live feed, transmission detail, audio playback with range requests.
- SSE `/events` fed by DB polling. The live feed updates without refresh.
- `/status` page and `/healthz`.

### M4 — Search and channels
- FTS5 table + triggers, `/search` page and `/api/transmissions` filters, snippet highlighting.
- Channels page with alias/priority/ignore editing.

### M5 — Hardening and deployment
- systemd unit (`Restart=on-failure`, `User=` a dedicated service account, `ProtectSystem=strict`, `ReadWritePaths=` the data dir).
- Retention job.
- Reconnect/backoff tests: unplug the scanner Ethernet, power-cycle the scanner, restart the NUC.
- Watchdog alert (optional): notify (e.g., via ntfy or email) when no transmissions arrive for N hours on a normally busy system, or when the scanner is disconnected for more than N minutes.
- `tailscale serve` setup documented in `deploy/`.
- Thermal soak test: 24 h with real traffic, checking for throttling and queue growth.

### Later / nice-to-have
- Multiple scanners (`scanner_id` is already in the schema).
- Re-transcribe with a better model on demand, or in bulk overnight.
- Tone-out / data-burst detection (label rather than transcribe).
- Export (CSV/JSON) of search results.
- Optional re-stream of live audio to the browser (the service owns the single RTSP session, so it would be the one to fan it out).

---

## 10. Testing strategy

- **Unit tests:** segmenter state machine (fixture-driven), µ-law decode, resampler, FTS queries, post-processing substitutions, the queue claim race (two workers, one row).
- **Replay integration test:** a short fixture session run through the whole pipeline with a fake STT backend (returns canned text) produces the expected rows, SSE events, and search results.
- **Hardware smoke test (manual):** `python -m scanner_transcriber run --config ...` against the real scanner for 10 minutes. Verify clip boundaries by listening, and verify clean teardown.
- **Benchmark regression:** keep `bench.py` runnable so model or runtime upgrades can be compared on the NUC.

---

## 11. Observability

- Structured logs (JSON lines) to journald: connection events, reconnects, RTP loss stats, segmenter decisions (at debug level), worker timings, errors.
- `/api/status` metrics: scanner connected, seconds since last status and last audio, RTP loss %, queue depth by status, rolling RTF, transcriptions/hour, CPU temperature and frequency (from `/sys/class/thermal` and `/proc/cpuinfo`), disk free.

---

## 12. Security and privacy

- Service binds to localhost only. Exposure is through `tailscale serve`, and access control is the tailnet ACL.
- Run as an unprivileged dedicated user with systemd sandboxing.
- Public-safety transmissions can include names, addresses, and medical details. Keep the page tailnet-only, and review legal and ethical considerations before any public exposure. Retention defaults keep audio short-lived.
- No secrets are needed in v1. If notification tokens are added later, load them from environment/credentials files, not `config.toml` in the repo.

---

## 13. Open questions (resolve during M0/M1)

1. Which STT backend/model meets RTF ≤ 0.5 with acceptable accuracy on the N5105? Is the OpenVINO/iGPU path worth its complexity?
2. Does the SDS200's pushed status stream reliably indicate squelch open/close, or must the segmenter rely on audio energy/VAD for boundaries?
3. What is the measured audio-vs-metadata latency offset (`audio_latency_ms`)?
4. Is the `sds200` PyPI library's license and API suitable to depend on, or should we implement the protocol ourselves from the Uniden spec?
5. Opus vs µ-law WAV for stored clips?
6. Stable channel key definition for trunked vs conventional systems (TGID-based vs frequency-based).
7. Should ignored channels be captured but not transcribed, or not captured at all?

---

## 14. References

- `sds200` Python library: https://pypi.org/project/sds200/ (source/wiki: https://github.com/stevenboyd78/sds200-python)
- SDS200 web control app (protocol notes: UDP 50536 commands, RTSP `au:scanner.au`, keepalive, single-session behavior): https://github.com/2E0LXY/SDS200
- SDS200 Home Assistant bridge (another reference implementation of control + audio): https://github.com/immakinbacon/sds200ha
- Uniden *SDS Series Remote Command Specification* (V2.00): obtain from Uniden's SDS200 support/downloads page
- Moonshine ASR: https://github.com/moonshine-ai/moonshine (verify current repo location/package name)
- parakeet.cpp: https://github.com/mudler/parakeet.cpp
- whisper.cpp (incl. OpenVINO encoder support): https://github.com/ggml-org/whisper.cpp
- SQLite FTS5: https://www.sqlite.org/fts5.html
- htmx SSE extension: https://htmx.org/extensions/sse/
- Tailscale Serve: https://tailscale.com/kb/1312/serve
