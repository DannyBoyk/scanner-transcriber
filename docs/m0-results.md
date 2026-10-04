# M0 results: STT feasibility on the NUC

**Date:** 2026-09-29/30 · **Hardware:** NUC 11, Celeron N5105 (4C/4T, SSE4.2, **no AVX/AVX2**),
8 GB, Intel UHD iGPU (Jasper Lake) · **Scanner:** SDS200 fw 1.21.00, P25 trunk
(Tri-County)

## Verdict

**The exit criterion is met.** Several models run faster than real time on this box. The best
choice is **Whisper via OpenVINO on the iGPU**:

- **Default for M2:** `whisper-small.en` int8 on the iGPU, with a vocabulary prompt.
  WER 0.20 (0.26 on the harder, edited clips), RTF 1.45, about 1 CPU core busy.
- **Parakeet TDT-CTC 110M** via sherpa-onnx (RTF 0.07, about 0.3 core) has two narrow jobs:
  **degraded mode** (the worker switches to it if the iGPU model won't load), and a **speech
  check** when Whisper outputs a known hallucination phrase. *Superseded:* this section
  originally proposed a backlog fallback worker. M2 planning dropped it, because traffic is
  7–10% talk time and the scanner's single receiver caps the load. See
  [`m2-plan.md`](m2-plan.md).

**Why the looser speed target is acceptable:** the design's RTF ≤ 0.5 target was replaced with
**RTF ≤ 2 on 4 threads** (see README). Measured traffic was about 7% duty cycle (216 s of speech
in 3,000 s). At RTF 1.45, a 5-second transmission is transcribed about 7 s after it ends, and
the queue only builds up above about 70% duty cycle. **Re-check this against a busy-period
recording.**

The iGPU rows were rescored after a reference typo was fixed (clip 0046: "Warrington" →
"Ashford"). The other rows moved by at most about 0.005 WER and keep their original scores.

## Held-out check (2026-09-30)

To check that the prompt's gain is real, it was tested on a second, unseen 1-hour recording
(`data/sessions/20260930T103726Z`). That recording was cut by the production M1 segmenter,
and 30 clips were drafted by large-v3-turbo **without** the prompt and then corrected by hand
(29 scored).

| Model (held-out, 29 clips) | RTF | WER | WER (17 edited) | Prompt-word insertions |
|---|---|---|---|---|
| **whisper-small.en int8 + prompt (iGPU)** | 1.71 | **0.254** | **0.336** | 2 (1 real leak) |
| whisper-small.en int8 (iGPU) | 1.61 | 0.392 | 0.518 | 1 |
| whisper-base.en int8 + prompt (iGPU) | 0.47 | 0.440 | 0.555 | 4 |
| whisper-base.en int8 (iGPU) | 0.44 | 0.455 | 0.655 | 1 |
| Parakeet TDT-CTC 110M (CPU, sherpa-onnx) | 0.07 | 0.459 | 0.527 | 1 |

What the held-out check shows:

- **The prompt holds up on small.en:** WER 0.39 → 0.25 on unseen audio. There was one real
  leak ("Victor, respond" for "Medic 3 responding"); the other flagged insertion was a
  spelling variant ("Charlie" vs "Charley"). The prompt also recovered a road name the human
  labeller first missed: clip 0104's reference was corrected to "1600 **Lakeside**
  Highway", and the table is rescored with it.
- **The prompt barely helps base.en** (0.46 → 0.44) and leaks more, so base.en was dropped.
- **Parakeet 110M matches base.en** on this audio, at 1/6 the time, on the CPU, and without
  inventing text. It replaced base.en as the fallback model (now used only for degraded mode and speech checks; see M2). It also runs alongside the
  iGPU worker without competing for the GPU.

## Method

- **Data:** one 50-minute live recording, cut into 92 transmission clips (median 1.8 s).
  50 clips were drafted by whisper large-v3-turbo (beam 5) and corrected by hand. 43 are
  scored and 7 were skipped as unintelligible.
- **Scoring:** WER/CER after lenient normalization (case, punctuation, and numbers as digits
  or words: "one oh six" = "106" = "1 0 6").
- **Speed:** RTF is transcription wall time ÷ audio duration, after one warm-up clip. `cpu_rtf`
  is CPU-seconds (all threads) ÷ audio seconds. Each candidate ran in its own process.
- **Bias check:** the references started as Whisper drafts, and 18 of the 43 were accepted
  unchanged, which favours Whisper-family models. The "edited" column scores only the 25
  clips that were changed. The ranking is the same there.

## Results (43 scored clips; 4 threads unless noted)

| Model | Runtime | RTF | cpu_rtf | WER | WER (edited 25) | RSS |
|---|---|---|---|---|---|---|
| whisper-small.en int8 + prompt | OpenVINO iGPU | 1.45 | 1.47 | **0.198** | **0.258** | 1.1 GB |
| whisper-base.en int8 + prompt | OpenVINO iGPU | 0.41 | 0.43 | 0.275 | 0.352 | 0.8 GB |
| whisper-base.en int8 | OpenVINO iGPU | 0.39 | 0.40 | 0.347 | 0.462 | 0.6 GB |
| whisper-small.en int8 | OpenVINO iGPU | 1.42 | 1.43 | 0.361 | 0.447 | 1.0 GB |
| whisper-small.en int8 | faster-whisper CPU | 2.10 | — | 0.351 | 0.462 | 1.2 GB |
| Parakeet TDT 0.6B v2 fp32 | OpenVINO EP, CPU | 0.43 | — | 0.394 | 0.496 | **5.8 GB** |
| whisper-base.en int8 | faster-whisper CPU | 0.63 | 2.29 | 0.384–0.408 | 0.511 | 0.5 GB |
| Parakeet TDT 0.6B v2 int8 (t2) | onnx-asr CPU | 0.52 | — | 0.458 | 0.553 | 0.9 GB |
| whisper-tiny.en int8 | faster-whisper CPU | 0.37 | — | 0.460 | 0.519 | 0.3 GB |
| Parakeet TDT-CTC 110M int8 | sherpa-onnx CPU | 0.07 | 0.27 | 0.525 | 0.598 | 0.3 GB |
| Moonshine base (t2) | ONNX CPU | 0.15 | — | 0.597 | 0.659 | 0.7 GB |
| Moonshine tiny (t2) | ONNX CPU | 0.07 | — | 0.599 | — | 0.4 GB |

Thread scaling: Parakeet 110M goes 0.17 → 0.10 → 0.07 at 1/2/4 threads. Moonshine and Parakeet
0.6B int8 are fastest at 2 threads and slower at 4. CPU temperature peaked at 60 °C, with no
throttling seen. Raw results are in `data/bench/<timestamp>/`.

**Does not work:** Parakeet on the iGPU. With FP32 it fails at initialization; with FP16 it
returns empty text.

## Findings

1. **faster-whisper is penalized on short clips.** Whisper always encodes a padded 30 s
   window, so RTF barely depends on clip length (1–3 s here). The iGPU absorbs that fixed cost
   well. It still keeps about one CPU core busy, which looks like the host waiting on the GPU.
2. **The vocabulary prompt is the single biggest accuracy lever** (−0.07 to −0.16 WER). But it
   was built from these same clips, so the gain is **optimistic**. It also **leaks** on noisy
   audio: base.en wrote "Uniform Northfield, Northfield" (the county name from the prompt) for "foxtrot, foxtrot", and small.en
   appended "one oh six". Small.en leaks less. Before trusting it, tune the prompt on a fresh,
   held-out recording, and add the phonetic alphabet (whisper hears "foxtrot" as "5-shot").
3. **Whisper hallucination risk remains.** No clip in the scored set was pure noise, so the
   `halluc` metric is empty. The drafting model produced "Thank you." on several faint clips.
   Dan confirmed some of those clips held real but faint speech. M2 needs a no-speech guard:
   empty output, repeated n-grams, or text that matches the prompt.
4. **Parakeet** is the best non-Whisper family and never invents text on noise. The 110M
   export is CTC-only, so it can't use sherpa hotwords; vocabulary fixes would have to come from
   the substitution map.

## Open questions from the design (§13)

| # | Question | Answer |
|---|---|---|
| 1 | Which backend meets the target? Is the iGPU worth it? | Whisper small.en via OpenVINO on the iGPU, with the prompt. **Yes**, it is worth it (see above). |
| 2 | Does status reliably show squelch? | Yes. `Property Mute="Mute/Unmute"` plus TGID/UnitID in PSI (about 4/s). Audio is also exact zeros while muted. |
| 3 | Audio vs metadata latency? | Within the 250 ms PSI interval (median about 0). Keep `audio_latency_ms = 0`. |
| 4 | Use the `sds200` library? | Yes. It's MIT-licensed, handles RTSP/RTP with statistics, and TEARDOWN is verified. Validated on fw 1.26.01; this scanner runs 1.21.00 without issues so far. |
| 5 | Opus vs µ-law WAV? | Not decided. `ffmpeg` is now installed, so Opus is possible. |
| 6 | Channel key? | Trunked P25: `sys:<system>|tg:<tgid>`. TGIDs are always present during voice. |
| 7 | Ignored channels? | Not decided. |

## Carry into M1 (segmenter)

- **Don't split on silence alone.** Exact zeros and a ±8 noise floor also occur *inside*
  unmuted transmissions, so pure audio thresholds split sentences. Keep the clip open while PSI
  shows the same TGID. (Refined in M1: one clip per key-up, a 400 ms debounce instead of
  hangtime, and a split on unit handoff. See [`m1-plan.md`](m1-plan.md).)
- **Clipped starts come from the scanner, not us.** Some transmissions start mid-word because
  the scanner unmutes late (`Link` → `Unmute`). No software fix.
- **No audio-level gate.** Low-level clips (−45 to −58 dBFS) are often real faint speech.
- **Busy talkgroups** (this sample): 1101 County Sheriff Dispatch, 1201 Town PD,
  1002/1620 County Fire/Rescue, and 65121 (unlabeled, likely a neighbouring county's dispatch).

## Next steps

1. Record a busy period (README tip) and a held-out session. Label about 30 clips from it to
   tune the prompt fairly and check for hallucination.
2. M1: segmenter + DB + ReplaySource, using the lessons above.
3. M2: OpenVINO Whisper worker (small.en + prompt; Parakeet for degraded mode), a no-speech/leak guard,
   and the substitution map.
4. M5 soak test: 24 h on the iGPU to check driver stability; Parakeet 110M is the degraded-mode model.
