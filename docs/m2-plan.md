# M2 plan: transcription worker

**Status (2026-09-30): done.** Every exit check passed. The 30-minute live run: 106 clips,
all transcribed, 0 errors, 0 packets lost; latency from transmission end to transcript was
p50 7.4 s, p95 11.8 s, max 18.2 s. What building it taught us is under "Learned while
building" at the end.

**Goal:** every clip M1 queues gets a transcript, normally within about 10 s of the
transmission ending. The transcript is stored with enough metadata to judge its quality, and
the system recovers from crashes and restarts on its own. There is no web UI yet (M3); a
small `status` command shows the queue.

**Exit criteria**

- **Live, 30+ minutes:** every clip reaches `done`, `skipped` or `failed` (no stuck rows).
  Median time from transmission end to transcript is 15 s or less.
- **Burst test:** replaying the 1-hour held-out session at max speed queues about 136 clips at
  once. The worker drains them in order with no failures, and `status` reports the backlog and
  latency correctly.
- **Crash test:** killing the worker mid-clip gets that clip re-queued and transcribed. A full
  restart loses nothing.
- **GPU-failure test:** with the iGPU made unavailable, the worker starts in Parakeet-only
  mode, logs a warning, and keeps transcribing.
- **Accuracy:** replaying the held-out session through the *real pipeline* (segmenter →
  worker) reproduces about the benchmark WER (≈0.25) when matched to the hand labels by
  timestamp.
- Unit tests (fake STT backend) pass: claim race, retries, stale-claim recovery, guards,
  substitutions and ignored channels.

## Decisions

- **One worker, one model:** whisper-small.en int8 on the **iGPU** (OpenVINO GenAI) with the
  vocabulary prompt from `[stt] prompt`. About 4.7 s per clip (see "Learned while building"),
  one CPU core busy.
- **No backlog fallback worker** (decided 2026-09-30). Measured traffic is 7–10% talk time.
  The worker only falls behind above about 59% sustained, and the SDS200's single receiver
  caps the load: it can only play one transmission at a time. A 30-minute incident at 70%
  talk time would put transcripts about 6 minutes behind at worst, and the queue drains
  afterwards. `status` measures queue depth and latency, so a fallback can be added later if
  a backlog is ever observed. The queue design doesn't change.
- **Degraded mode instead:** if the iGPU model fails to load, the same worker switches to
  **Parakeet TDT-CTC 110M on the CPU** (sherpa-onnx, RTF 0.07) and logs a loud warning. It
  retries the GPU on the next restart.
- **One environment:** checked on 2026-09-30, `openvino-genai` and `sherpa-onnx` run together
  in one process, so the worker doesn't need `.venv-ov`. New `worker` extra:
  `openvino-genai`, `huggingface_hub`, `sherpa-onnx`, `sherpa-onnx-core`. The old benchmark
  runtimes move to the `bench` extra.
- **Atomic claims:** SQLite 3.53 supports `UPDATE … RETURNING`, so claiming a job is one
  statement. That keeps a second worker possible later without redesign.
- **No confidence score from OpenVINO Whisper** (`scores` is always `[1.0]`). Whisper also
  hallucinates without the prompt: clip 0104 became "Thank you very much and happy 2021." So
  quality is judged by the guards below.

## Design

### Worker

A single process (`multiprocessing`, spawn) started and supervised by `run`: restarted with
backoff if it dies. It also runs on its own with a new `worker` command, for example to
transcribe a replayed session.

Claiming, highest priority first, then oldest:

```sql
UPDATE transmissions SET status='processing', claimed_at=?, claimed_by=?, attempts=attempts+1
WHERE id = (SELECT id FROM transmissions WHERE status='queued'
            ORDER BY priority DESC, started_at ASC LIMIT 1)
RETURNING id, audio_path, ...
```

It polls about every 0.5 s when the queue is empty, which is simple and robust, and the same
approach the design doc chose for the SSE feed.

### Per-clip pipeline

1. Load the WAV, resample 8 → 16 kHz (existing `audio.resample`).
2. Whisper with the prompt (or Parakeet alone in degraded mode).
3. **No per-clip cross-check.** Step 1 found that Parakeet agreement doesn't predict bad
   Whisper transcripts (see "Cross-check analysis" below).
4. **Guards** (each adds an entry to `flags`, which the UI can show):
   - `empty`: no text → `no_speech=1`.
   - `repeat`: collapse repeated phrases ("one oh six, one oh six, one oh six…").
   - `hallucination`: output matches a small list of known Whisper phrases ("Thank you.",
     "Thanks for watching", …) → run **Parakeet as a speech check** on that clip only. If
     Parakeet also hears nothing (or almost nothing), blank the transcript and set
     `no_speech=1`. Otherwise keep Whisper's text, flagged, and store Parakeet's text as
     `alt_transcript`.
   - `prompt_leak`: a run of prompt words (e.g. three or more phonetic-alphabet words, or a
     prompt phrase) → retry once without the prompt and keep the version with fewer
     prompt words. Flagged either way.
5. **Substitutions:** case-insensitive whole-word replacements from `[stt.substitutions]`
   (e.g. "Charley" → "Charlie"). Applied to the display text only; raw model output is kept.
6. Write `transcript`, `raw_transcript`, `alt_transcript` (speech checks only), `flags`,
   `no_speech`, `stt_backend`/`stt_model`, `stt_ms`, `transcribed_at`, and `status='done'`.

### Failure handling

- **Exceptions:** the row returns to `queued` with `error` set; after `max_attempts`
  (default 3) it becomes `failed`.
- **Stale claims** (worker crashed or was killed): rows stuck in `processing` longer than
  `claim_timeout_s` (default 120) go back to `queued`, at startup and every minute.
- **Model load:** OpenVINO's `CACHE_DIR` caches the compiled GPU model under
  `data/ov-cache`, so restarts take seconds instead of about 50 s.
- **The iGPU won't start:** degraded Parakeet-only mode (above).

### Ignored channels

`channels.ignored = 1` → the clip is saved with `status='skipped'` (reason `ignored`) and
never transcribed. Hard ignore stays Avoid on the scanner.

### Schema changes (`migrations/0002_worker.sql`)

- `transmissions`: add `raw_transcript`, `alt_transcript`, `flags` (JSON list),
  `claimed_by` and `transcribed_at`. `confidence` stays NULL: neither model gives a usable
  score.
- Add `alt_transcript` to the FTS index (it's only filled by speech checks).

### Config (`[stt]`, `[queue]`)

```toml
[stt]
model = "OpenVINO/whisper-small.en-int8-ov"
device = "GPU"
degraded_model = "sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000-int8"  # CPU, if the GPU fails
max_attempts = 3
prompt = """..."""             # existing
[stt.substitutions]
"Charley" = "Charlie"

[queue]
claim_timeout_s = 120
```

### `status` command

Queue depth by status, transcripts per hour, median and 95th-percentile latency (end of
transmission → transcript), mean RTF, share of rows flagged, worker mode (GPU or degraded),
and whether the worker is alive. The M3 `/status` page will show the same numbers. It's also
the evidence for whether a backlog fallback is ever needed.

## Cross-check analysis (step 1, done 2026-09-30)

Tested whether Whisper–Parakeet agreement could serve as a per-clip confidence score. Data:
the existing small.en+prompt and Parakeet outputs on both labelled sessions (72 clips, 12
where Whisper's WER was above 0.5).

- **Character similarity:** dominated by number formatting ("135" vs "one hundred and
  thirty five"). The lowest-agreement clips were mostly *correct* Whisper transcripts.
- **Word overlap, number words ignored:** to catch half the bad transcripts (6/12) you would
  flag 19 clips, 13 of them fine. Catching 9/12 flags 30 clips, 21 of them fine.
- **Why:** Parakeet is too inaccurate on this audio (WER about 0.46) to judge Whisper; most
  disagreements are its own errors.

**Decision:** no per-clip cross-check and no agreement-based confidence. Parakeet is used
only as (a) the degraded-mode model and (b) a targeted speech check when Whisper outputs a
known hallucination phrase.

## Tests

- **Queue:** two processes and one row → exactly one claims it; priority then age ordering;
  stale-claim recovery; retries end in `failed`; ignored channels are skipped.
- **Guards:** table-driven cases for empty, repeats, known hallucinations (speech check
  hears nothing vs. hears speech), prompt leak, and substitutions.
- **Degraded mode:** a backend whose load raises → the worker switches to the fallback
  backend and records it.
- **Integration:** a synthetic session through `run --replay` plus the worker using a
  **fake STT backend** gives `done` rows with the expected text and flags. A killed worker
  gets its row recovered.
- **Real models (local only):** replay the held-out session through the full pipeline, match
  clips to the hand labels by time overlap, and report WER, latency and flag rates.

## Order of work

1. ~~Cross-check analysis~~ done: dropped (see above).
2. Packaging: the `worker` extra, and check the main `.venv` runs both models.
3. Migration `0002`, queue functions (claim, finish, fail, recover), unit tests.
4. Worker process: backend + degraded mode, guards, substitutions, fake-backend tests.
5. Wire into `run` (spawn, supervise, restart) + the standalone `worker` and `status`
   commands.
6. Held-out replay through the real pipeline (accuracy + burst test), then a 30-minute live
   run, a kill test and the GPU-failure test.

## Learned while building (2026-09-30)

- **Cost is per clip, not per second.** Whisper always processes a padded 30-second window, so
  every clip takes about 4.7 s on the iGPU (RTF only looks like 1.7 because clips average
  about 2.5 s). Capacity is about **12.7 clips per minute**; the busy held-out hour produced
  about 2.3 per minute.
- **Hallucination list:** "okay", "so", "oh", "thanks" and "bye" were removed; they're real
  replies on the radio. A genuine "Okay." was being blanked in the held-out replay. The speech
  check now blanks only when Parakeet hears **nothing**, not "one word or fewer".
- **Dead-worker recovery:** at startup, clips claimed by a worker process that no longer
  exists (`claimed_by` = host:pid) are re-queued immediately. The 120 s stale-claim timeout
  is only the backstop.
- **Model loading is offline-first:** the local Hugging Face copy is used if present, so a
  restart doesn't need the internet. The GPU compile cache (`data/ov-cache`) brings worker
  start-up to about 3 s.
- **`evaluate SESSION`** scores a replayed, hand-labelled session by time overlap, so labels
  survive segmenter changes. Held-out result through the full pipeline: **WER 0.263**
  (benchmark 0.254).
- **Test results:** burst of 136 clips drained in 12 min with no failures; `kill -9`
  mid-clip was recovered on restart; GPU unavailable → degraded mode in 1.9 s, clips keep
  flowing.
- **Config:** `config.toml` files copied before M2 have no `[stt]` section, so no prompt. Copy
  the `[stt]` section from `config.example.toml`.

## Out of scope

Web UI and SSE (M3), search UI and channel editing (M4), systemd, retention and the 24-hour
soak test (M5). A backlog fallback worker, unless `status` shows one is needed.
