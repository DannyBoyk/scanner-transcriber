# Development notes

Background for working on the code: the benchmark tooling from M0, the labelling workflow,
and how to read the numbers. Day-to-day use is in the [README](../README.md); the milestone
plans and results are the other files in this folder.

## Benchmark setup (OpenVINO venv)

The OpenVINO/iGPU backends use a separate venv, because `onnxruntime-openvino` conflicts
with `onnxruntime`:

```bash
uv venv --python 3.12 .venv-ov
VIRTUAL_ENV=.venv-ov uv pip install onnxruntime-openvino openvino-genai "onnx-asr[hub]" \
    numpy soxr jiwer psutil huggingface_hub
VIRTUAL_ENV=.venv-ov uv pip install --no-deps -e .
.venv-ov/bin/python -m scanner_transcriber bench ...   # e.g. --candidate ovwhisper:OpenVINO/whisper-small.en-int8-ov:device=GPU
```

The iGPU also needs `intel-opencl-icd` (the Jasper Lake GPU uses `intel-opencl-icd-legacy`),
and the user must be in the `render` group; log in again after adding it.

## Labelling and benchmark commands

All run as `uv run python -m scanner_transcriber <command>`.

| Command | What it does |
|---|---|
| `record --duration SECONDS` | Records raw audio and status to `data/sessions/<timestamp>/`. Ctrl-C stops it cleanly. Not while the service runs (one audio session). |
| `run --replay SESSION [--drain]` | Runs capture and transcription on a recording instead of the scanner. |
| `evaluate SESSION` | Scores the database's transcripts of a replayed, hand-labelled session. Useful when tuning the prompt. |
| `sample-recent [-n 100]` | Makes a labelling session from recent recordings: a deliberate mix of short replies, unit-ID openers and calls naming `--place` (e.g. your county). Then `draft … --min-ms 0` (or other drafts) and `label`. Once labelled, prompt tests include it. |
| `cut SESSION` | Splits a recording into per-transmission clips (`clips/`, `clips.jsonl`). |
| `draft SESSION` | Drafts reference transcripts with a large, slow model (whisper large-v3-turbo). |
| `label SESSION --host IP` | Serves a web page for correcting drafts, writing `refs.jsonl`. It has no auth, so stop it when done. |
| `bench SESSION` | Benchmarks STT models: speed (RTF), accuracy (WER), memory and CPU temperature. |

## Tip: record long sessions inside `screen` or `tmux`

Long jobs die when your SSH session closes unless they're detached. Use:

```bash
screen -S rec
uv run python -m scanner_transcriber record --duration 3600
# detach: Ctrl-A then D      reattach later: screen -r rec
```

(or `tmux new -s rec` / detach with `Ctrl-B D` / `tmux attach -t rec`). The same applies to
running Claude Code itself during long benchmarks.

### Record a busy period

The first labeled recording was an ordinary weekday evening: about **7% of the time** had
someone talking (3.6 minutes of speech in 50 minutes). To size the system for the worst
case, record an hour or more during a **busy stretch**, such as a Friday or Saturday night, a
storm, or a major incident. Then run `cut` on it and read `active_s` / `audio_s` in
`cut_report.json` to get the peak duty cycle.

## Reading the benchmark numbers

**RTF (real-time factor)** is how long transcription takes compared with the length of the
audio. At RTF 0.5, a 10-second clip takes 5 seconds; at RTF 2, it takes 20 seconds. Lower is
faster.

**WER (word error rate)** is the share of words the model got wrong (substituted, dropped
or added) compared with the corrected transcript. 0.0 is perfect, and 0.25 is about one word
in four wrong, which is usually still readable and searchable. Numbers like "one oh six" and
"106" are normalized so they match. **CER** is the same idea counted in characters.
**halluc** is the share of no-speech clips where the model invented text.

### How fast is fast enough?

The design doc's target of RTF ≤ 0.5 was a cautious default. Scanner traffic has long gaps,
and the transcription queue catches up during them:

- At 7% duty cycle, the system only falls behind on average if RTF exceeds about 13.
- The real limits are **latency** (at RTF 2, a 5-second transmission is transcribed about
  10 seconds after it ends) and **busy periods**, when a backlog can build.
- Working target: **RTF ≤ 2 on 4 threads**, leaving about 4× headroom over today's traffic.
  Revisit this after measuring a busy period (see above).
- In practice the worker's cost is **per clip, not per second**. Whisper always processes a
  padded 30-second window, so each clip takes about 4.7 s on the iGPU whatever its length.
  That's about **12.7 clips per minute**; a busy hour so far produced about 2.3 per minute.
  `status` shows the live latency.
