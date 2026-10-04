# M6 plan: Listen live (a continuous audio stream)

**Status:** step 1 (spike) **passed** on 2026-10-01. Dan's iPhone kept playing the test
stream (MP3, 16 kHz mono, 32 kbit/s, beep every 5 s) with the screen locked, "without a hitch".
Range probes get `200` and the endless stream, like Icecast. The encoder is `lameenc`, the same
LAME as `ffmpeg`, but in-process. Decision: keep plain MP3, no HLS needed.

Steps 2–4 were **built** on 2026-10-01:
- the shared `Station` in `web/stream.py`;
- `/stream.mp3`, `/listen`, the header 🎧 link (shortened to "TGs" on phones so the header
  still fits), and a "Live stream" line on the Status page;
- 7 tests in `tests/test_stream.py`.

End to end against the real recordings: 7 transmissions (18.5 s) came through as 18 s of
audible stream, peaking at -16 dBFS (levelled). Deployed. Dan's real-use test (phone + computer) passed after two fixes: Safari's two
connections now count as one listener, and the page no longer reconnects on Safari's `stalled`
event (swapping the source while locked had stopped playback).

**Load test (2026-10-01), 10 `curl` listeners for 10 min against the live web:**
- web process CPU median 3.3% of one core (peak 4.1%), against 2–3% with 1–2 listeners;
- memory flat at 137–138 MB;
- every listener got exactly 2,407,968 bytes (4.01 KB/s = real time at 32 kbit/s), the same
  audio, no drops;
- the stream decoded cleanly (602 s, 45 s of speech, peak -15.9 dBFS);
- the site showed 10 listeners (11 with Dan).

M6 done. Deploying needs only a restart of
the web service. It waits until the M5 soak (#8/#9) finishes, unless Dan decides otherwise.

**Goal:** a "Listen live" button that plays the scanner's traffic as **one continuous audio
stream**, like Broadcastify. A phone keeps playing it when the screen is locked or the
browser is in the background. The same URL works in VLC and other players. Several people can
listen at once.

**Why the existing player can't do this:** the page player plays one clip file per
transmission, and JavaScript starts the next clip. Once an iPhone locks, Safari pauses or
throttles the page's JavaScript, so playback stops after the current clip. iOS keeps playing a
single never-ending stream (internet-radio style), so the server has to produce one.

## Decisions (Dan, 2026-09-30)

| Topic | Decision |
|---|---|
| What it plays | **Live only**: from "now", no catch-up. |
| Delay | **Let it build:** no speed-up or skipping. In practice it stays at about one transmission's length (see "How it sounds"). |
| Content | **Everything, like the scanner's own speaker:** all talkgroups (also ones hidden from the live page or set to "Transcribe: off"), and also clips judged "no speech". No filters. |
| Launch | **Its own button and page**, separate from the existing player ("purposefully launched"). |
| Listeners | **Several at once.** |
| Lock screen | Static text (page title) with play/pause is fine. No per-transmission text. |
| Approach | Built from the saved clips in the **web process** (option 1 in the discussion). Capture is not touched. Possible later: HLS as a second output; a true real-time relay from capture only if the few seconds of delay bother us. |

## How it works

```
capture process (unchanged)          web process
───────────────────────────          ─────────────────────────────────────────────────────
saves clip WAV + DB row  ──────────▶  Station (one asyncio task, shared by all listeners)
                                        • picks up new clips (polls the DB every 0.5 s)
                                        • WAV → resample → MP3 encode (in a helper thread)
                                        • encoded silence when idle, paced to real time
                                        • pushes each MP3 chunk to every listener's queue
                                                 │        │        │
                                              listener  listener  listener
                                              GET /stream.mp3: an endless streaming response
```

- **No thread per listener.** uvicorn runs one asyncio event loop. Each connection is a
  coroutine, the same pattern as the live page's `/events` stream. Encoding happens **once**,
  whatever the number of listeners. Each extra listener only costs copying bytes (32 kbit/s,
  about 14 MB/hour).
- **The station starts with the first listener and stops 30 s after the last one leaves**, so
  nothing runs while nobody listens. Its "now" is the newest transmission id when it starts:
  only clips created after that are played.
- **Clip selection:** every transmission row with an `id` above the last one queued and an
  `audio_path`, in `id` order (which is the order capture saved them). No talkgroup, hidden,
  ignored or no-speech filtering. If a file is missing (retention, clean start), it's skipped.
- **Timing:** clips play back to back, separated by a short pause (0.4 s), with real silence
  in between when nothing is queued. Clips arrive when a transmission ends, and the scanner
  only plays one at a time, so audio can't pile up faster than real time. A backlog only
  forms in rapid back-and-forths and drains in the next quiet moment.
- **Pacing:** the station tracks "stream time" against the wall clock. It keeps the
  produced audio about **2 s ahead** of real time, so players can buffer, and sleeps
  otherwise. Silence is real encoded audio (MP3 frames of zeros), because players treat a
  stalled byte flow as a dead stream.
- **Listeners:**
  - Each listener has a bounded `asyncio.Queue` of MP3 chunks.
  - On connect, it gets the last ~2 s from a small ring buffer (fast start), then live chunks.
  - On disconnect, the generator's `finally` removes it.
  - If a listener's queue fills (about 20 s backlog: a dead connection that hasn't closed),
    it is dropped, so it can't hold the station back.
- **Audio format:** MP3, mono, 32 kbit/s. The scanner's 8 kHz is resampled to **16 kHz**
  (MPEG-2 Layer III), because 8 kHz MP3 (MPEG-2.5) isn't reliably supported by Safari. We
  already resample with `soxr` for the models.
- **Encoder:** `lameenc` (a pip wheel wrapping LAME, encoding in-process). The fallback is one
  long-lived `ffmpeg` subprocess fed PCM on stdin (ffmpeg is already installed). Choosing
  between them is part of step 1. Either way it's one encoder for the whole station.
- **HTTP:** `GET /stream.mp3` with `Content-Type: audio/mpeg`, `Cache-Control: no-store`,
  `X-Accel-Buffering: no`, no `Content-Length` (chunked). It's read-only and public like the
  rest of the site; over Tailscale it gets HTTPS via `tailscale serve`.
- **iOS detail to verify first:** Safari often opens media with a `Range: bytes=0-1` probe
  and may open a second connection. Internet-radio servers answer `200` with the stream and
  ignore the range; step 1 checks that this works on Dan's iPhone before anything else is
  built. Probe connections that close immediately just come and go as listeners.

## The page and button

- **"Listen live" in the header**, next to Live / Search / Talkgroups / Status. It opens
  `/listen`.
- **`/listen`:** a big play/stop button (iOS only starts audio after a tap), static text
  ("Listening live: everything the scanner hears, a few seconds behind"), the number of
  listeners, and the scanner and capture status (the same status dot as the header).
- **The page's `<audio>` points at `/stream.mp3`.** A little JavaScript:
  - **Reconnect:** if the stream errors or stalls while the page is visible (network change,
    web restart), reconnect after 3 s.
  - **Media Session:** title "Scanner – live", play/pause only.

  While the phone is locked, playback doesn't depend on any of this JavaScript.
- **Nothing on `/listen` interacts with the existing player.** Starting the stream stops any
  clip the page player was playing on that page, and vice versa, so two audio sources never
  overlap.
- **Status page:** a "Stream" line with the current listener count, and the station
  state (idle / playing).

## Steps

1. **Spike, about 1 hour:** a throwaway `/stream.mp3` that streams a test tone and silence
   through the chosen encoder. Dan opens it on the iPhone, locks the screen, and confirms it
   keeps playing for about 10 min. Also check VLC and desktop Firefox and Chrome. Pick
   `lameenc` or `ffmpeg`. **Go/no-go for the approach.** If iOS won't play it, the fallback
   is HLS (option 3).
2. **Station + listeners** (`web/stream.py`): clip pickup, resample/encode, pacing, silence,
   fan-out, ring buffer, slow-listener drop, start/stop with listeners.
3. **Endpoint, page, header button, status line**, and the reconnect JavaScript.
4. **Tests** (below), then docs: README, this file's results.
5. **Deploy** (restart the web service only), after the soak. Then a real-use check: Dan
   listens with the phone locked through a busy stretch, with a second listener at the same
   time.

## Tests

- **Unit (pytest), with a fake clock and fake clips:**
  - New clips are played in `id` order, including hidden, ignored and no-speech ones.
  - Clips from before the station started are not played.
  - Missing files are skipped.
  - Silence fills idle time at real-time pace.
  - Back-to-back clips get the pause and the backlog drains.
  - Two listeners receive identical bytes.
  - A late joiner gets the ring buffer and then live chunks.
  - A stalled listener is dropped without affecting others.
  - The station stops after the last listener plus 30 s, and restarts at the new "now".
- **HTTP:** `TestClient` reads a few seconds of `/stream.mp3`. Check the headers, that the
  bytes parse as valid MP3 frames (frame-sync and header check), and that a `Range: bytes=0-1`
  request still gets audio.
- **Browser check** (`tests/ui/stream_check.py`, run by `run_all.sh`): `/listen` → click
  play → the `<audio>` `currentTime` advances and keeps advancing across a newly inserted
  clip. Stopping it, then using the existing player, doesn't overlap.
- **Load:** 10 simultaneous `curl` listeners for 10 minutes on the NUC. Measure the web
  process's CPU and RSS, and confirm all 10 get the same byte rate. Expected: one encoder at
  well under 5% of one core. The listeners add almost nothing.
- **Real device (Dan):** iPhone Safari, phone locked for 10+ minutes, through a busy period.
  Switch between Wi-Fi and cellular to see how it recovers (it may stop and need a tap; see
  Risks).

## Risks and limits

- **Range probing / iOS quirks:** step 1 exists to find out before we build the rest.
- **Network change while locked:** with a plain MP3 stream, iOS may stop when the connection
  drops (Wi-Fi → cellular) and needs a tap to resume. The reconnect JavaScript only helps
  while the page is visible. HLS handles this better, so if it's a real annoyance, add HLS as
  a second output (`ffmpeg -f hls` fed from the same station).
- **Delay:** each transmission is heard right after it ends, typically 1–10 s behind the
  radio. A true real-time relay would need audio piped out of capture; deliberately not done
  now.
- **Web restarts** (deploys) cut all streams. The page reconnects while visible; locked
  phones need a tap.
- **Exposure:** like the rest of the site, anyone who can reach the web can listen. That's
  fine on the LAN and tailnet; nothing changes there.

## Effort

About a day: spike 1 h, station 3–4 h, page and status 1–2 h, tests 2 h, docs 30 min.


## Known issue and future work: cellular / CarPlay (2026-10-01)

**Seen:** Dan listened in the car (iPhone over Tailscale on mobile data, CarPlay, phone locked).
It played for about 17 minutes (12:53–13:10), then went silent while the car's elapsed-time
counter reset to zero and kept counting.

**From the listener log:**
- 13:10:11: the phone opened a **new** connection (new page token) while the old one was
  still open and streaming.
- 13:10:38: the new connection closed after 27 s.
- 13:10:39: the old one closed after 1,006 s.
- After that there were no more attempts. The server never dropped the listener.

**Likely cause:**
1. A cellular hiccup outlasted the 2 s head start (`AHEAD_S`).
2. Safari reported a playback error.
3. The page reconnected with a new source.
4. iOS doesn't let a locked or background page start new playback, so it stopped that
   stream about 27 s later.

Anything that needs the page's JavaScript to restart the stream fails while the phone is
locked.

**Options (not scheduled; Dan: future work):**
1. **Bigger head start** (`AHEAD_S` about 10 s, plus a matching quick-start buffer). Rides out
   cellular gaps up to about 10 s; costs about 8 s more delay. One setting plus a web restart.
   Partial fix.
2. **HLS output: the proper fix.** Serve a rolling playlist of short segments (`ffmpeg -f hls`
   fed from the station's audio). The phone's own media player fetches them and retries
   failed segments, with no page JavaScript involved, so it survives dead zones and network
   changes while locked or in CarPlay. About 6–12 s delay; about half a day of work. The page
   would use HLS on iPhone; VLC and desktops keep `/stream.mp3`.
3. **Client-side diagnostics:** the page reports its media events (error codes, reconnects,
   refused play) to the server. Only works while its script runs.
