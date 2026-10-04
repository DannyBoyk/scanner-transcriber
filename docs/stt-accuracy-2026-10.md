# Transcription accuracy work, October 2026

Dan's report: the live model (whisper-small.en int8 on the iGPU, with the vocabulary prompt)
mostly fails on **unit numbers**, **short replies like "Direct"**, and **the county's name** (written "Northfield" here).

## Test sets

- **2026-10-01 set**: 100 recent clips picked by `sample-recent`: 35 short replies (≤ 1.5 s),
  about 35 unit-ID openers or county-name calls, 30 others. Dan corrected the live model's text,
  with Parakeet's text shown as a second opinion. Result: 89 usable, 11 skipped as
  unintelligible, 7 marked bad cut (mostly "clipped at the front"). The live score may be
  slightly flattering, because the drafts were its own output.
- **Held-out M0 set**: 72 clips from the 2026-09-29/30 sessions, drafted by large-v3-turbo and
  corrected by hand. Not used for any tuning here.
- **Scoring change** (`bench.normalize`): each number is now one token. "one thirty-five" ==
  "one three five" == "135", and "one hundred and six" == "106". A wrong unit ID counts as
  one error, not one per digit. Older WER figures aren't directly comparable.

## Baseline (2026-10-01 set, live model): WER 0.199

| Clips | WER | Clips with an error |
|---|---|---|
| Short replies (46) | 0.31 | 13 |
| Unit-ID openers (27) | 0.18 | 16 |
| County name (2) | 0.07 | 1 |
| Other (14) | 0.18 | 10 |

- **First words are wrong 28% of the time**, against 11% from the 4th word on.
- **Short-reply misses:**
  - "Direct" ×6 → "During", "Okay", "Friends", "Good", "Thank you";
  - bare unit IDs: 126 → "1.6", 621 → "Victor, I want…", 86 → "E6", 904 → "Alpha";
  - "10-4" → "Denver".
- **Parakeet 110M scores 0.557 overall, but heard 126, 621 and 106 correctly** where Whisper
  didn't.

## Experiments

| Variant | 2026-10-01: short (≤ 2 s, 52) | 2026-10-01: all (89) | Held-out: short (17) | Held-out: all (72) |
|---|---|---|---|---|
| Current (prompt v4) | 0.361 | 0.199 | 0.459 | 0.329 |
| **+ 0.5 s leading silence on clips ≤ 2 s** | **0.299** | **0.184** | **0.426** | **0.325** |
| 0.5 s leading silence on *every* clip | 0.299 | 0.218 | — | — |
| Short-reply prompt on short clips | 0.412 | — | — | — |
| No prompt on short clips | 0.536 | — | — | — |
| Parakeet's number when Whisper heard none (≤ 1.5 s) | — | 0.194 | — | — |
| Parakeet as tie-breaker between variants | — | 0.184 | — | — |

- **Adopted:** leading silence for clips ≤ 2 s (`[stt] lead_silence_ms = 500`,
  `lead_silence_max_clip_ms = 2000`). Every live, re-transcribed and prompt-test clip goes
  through `Transcriber.transcribe`, so they all get it. It costs nothing extra: Whisper pads
  every clip to 30 s anyway.
- **The prompt is essential:** with no prompt, short clips get far worse (11 worse, 1 better).
- **The short-reply prompt** fixed exactly the bare replies ("Direct", 106, 126, 621, 86,
  10-4, 117). But it cut off words after a number ("101 on duty" → "101.") and once looped.
  Clip length doesn't separate the two cases. Not adopted.
- **The Parakeet rules** were marginal, or cost 2–3× the compute for the same gain as the
  silence. Not adopted.

## Future ideas (parked 2026-10-01, Dan: try later)

Not planned work; candidates to try when accuracy becomes the priority again.

- **"Direct" and bare unit IDs.** The remaining targeted idea: on short clips, score a known
  list (common replies plus the unit IDs that occur in this county's traffic) against the
  audio and pick the best fit. This tolerates a clipped first syllable better than open
  transcription.
- **The county name.** The current prompt names it only in "Northfield Hospital". Try adding the bare
  word and the radio pattern ("Northfield to Medic 1"), measured on a batch with more
  county-name calls (`sample-recent --place Northfield` again).
- **Clipped starts** (7 of 100 marked): partly the radio itself (people talk as they key up).
  Worth checking whether the segmenter's 300 ms pre-roll could start earlier.
