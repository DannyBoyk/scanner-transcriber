# SDS200 network audio lockout (RTSP): what we've seen and what others report

**The problem:** the SDS200's network audio (RTSP on TCP 554, RTP on UDP) can get stuck. Once
stuck, the scanner **refuses TCP connections on 554** until it's power-cycled. The UDP control
channel (50536: status, commands) keeps working throughout. It's a scanner firmware
weakness: nothing on the NUC holds the session. During the unplug test the NUC had no TCP
connection to the scanner at all (`ss -tan dst 192.168.1.11` was empty).

## What we measured (firmware 1.21.00)

| Date | Trigger | Result |
|---|---|---|
| 2026-09-30 | `kill -9` of capture (no TEARDOWN) | systemd restarted capture in 6 s; 554 refused for 16 min, until a power cycle |
| 2026-09-30 | Scanner Ethernet unplugged about 2 min | Capture noticed within 5 s. After the replug, UDP control answered, but 554 was refused until a power cycle |
| 2026-09-30 | Graceful stop/restart (SIGTERM) | TEARDOWN sent; back in about 13 s, no lockout |
| 2026-09-30 | Scanner power cycle | Clean reconnect at capture's next retry |
| 2026-09-30 19:09 | Unplugged about 25 s | **Recovered by itself**, 9 s after the replug (details below) |
| 2026-09-30 19:12 | Unplugged about 2 min | **Locked out**, as predicted: 554 refused after the replug |
| 2026-09-30 19:14:52 | `MSM,0` over UDP on the locked scanner | Reply `MSM,OK`; the scanner **rebooted** (Dan saw it; about 5 s off the network). Capture's own retry reconnected at 19:16:42 (the first retry, at 19:15:40, was refused, most likely because of our bare TCP probes to 554 right after the reboot: see note) |
| 2026-09-30 19:17:02 | `MSM,0` again, while streaming | Rebooted; capture reconnected by itself 21 s later, no lockout |
| 2026-09-30 19:34 | Unplugged about 2.5 min, **with prevention + recovery** | Socket parked and stayed `ESTAB` (9 retransmits by 19:36:51). The parked TEARDOWN was answered 224 s after parking, about 80 s after the replug. The next connect was accepted, but the scanner didn't answer OPTIONS within 5 s; our normal close left it half-open (`FIN-WAIT-2`), holding the slot → refused. **Automatic `MSM,0`** after 3 min of audio down (19:41:17); audio back at 19:41:39. About 4.5 min from replug to audio, hands-off |
| 2026-09-30 19:47 | Unplugged about 2.5 min, **after adding reset-on-close, 5 s settle, 10 s RTSP timeout** | Parked TEARDOWN answered at 19:51:00 (226 s parked, about 90 s after the replug); an earlier attempt failed without leaving a half-open connection. Audio back at 19:51:42, at capture's next retry. **No reboot; 2 min 12 s from replug to audio** |
| 2026-09-30 19:59 | Unplugged about 2.5 min, **plus reconnect as soon as the parked TEARDOWN is answered** | TEARDOWN answered at 20:02:57.9 (about 80 s after the replug, at the next TCP retransmit); audio back at 20:03:04.5, 6.6 s later. **No reboot; 1 min 24 s from replug to audio.** The rest is the kernel's retransmit backoff (up to 2 min apart), which we can't shorten from the application |
| 2026-09-30 19:53 | Admin "Reboot scanner" button | TEARDOWN sent, then `MSM,0`; audio back 21 s later. Events: the Admin request, "Rebooting the scanner (requested from the Admin page)", "Scanner reconnected after 21s" |

### Why about 60 seconds: the NUC's kernel gives up on the close

Socket states (`ss -tan dst 192.168.1.11`) during the two unplugs showed the mechanism:

1. **About 5 s after the unplug**, capture declares the source stale. It tries a TEARDOWN,
   which can't get through, and closes its RTSP socket. The socket goes to **`FIN-WAIT-1`**:
   the kernel keeps retransmitting our FIN to the scanner, with exponential backoff.
2. **Short outage (25 s):** after the replug, a retransmitted FIN reaches the scanner. It
   drops the old session and accepts a new one at once (for a moment we saw
   `ESTAB,FIN-WAIT-1`). Audio is back in 9 s.
3. **Long outage (2 min):** the kernel retries an orphaned socket's FIN only
   `tcp_orphan_retries` times. At 0, Linux's default is 8, which puts the last retransmit
   about 50 s after the close. Then it discards the socket: `FIN-WAIT-1` disappeared at
   19:14:14, about 110 s after the close, while the cable was still out. The scanner never
   learns that the connection ended. It keeps the session and refuses new connections,
   and it never sends anything that would make our kernel reset it.

So the "about 60 s" in the forum reports is the Linux (and presumably Windows) FIN-retry
window, not a scanner timer.

**Note: don't probe 554 with a bare TCP connect.** Right after the first `MSM,0` reboot, two
`/dev/tcp` checks connected to 554. The scanner acknowledged our close but never closed its
side (both sat in `FIN-WAIT-2` on the NUC). Capture's next attempt was refused, and the one
after, about a minute later, succeeded. A bare connection seems to take the single slot
until the scanner times it out.

## What others report

- **Root cause (ProScan's author):** if the client shuts down abruptly, or the Ethernet link
  breaks or is intermittent, the scanner's RTSP module never gets the TEARDOWN and "stops
  communicating". "The only way to restore the RTSP communications is a scanner power
  cycle." ProScan 17.5+ detects lost LAN audio after 30 s and can reboot the scanner after
  5 minutes. That's monitoring plus a reboot, not a fix.
  [RadioReference: "Network connection causing SDS200 to go deaf"](https://forums.radioreference.com/threads/network-connection-causing-sds200-to-go-deaf.430715/)
- **About a 60-second threshold:** a link break of roughly 60 s or more needs a power cycle,
  while about 30 s may recover on its own. Restarting the PC or the software doesn't help.
  [Audio goes away SDS200](https://forums.radioreference.com/threads/audio-goes-away-sds200.468502/),
  [ProScan Audio "issues"](https://forums.radioreference.com/threads/proscan-audio-issues.413721/),
  [Proscan / SDS200 connection dropping on internal WiFi](https://forums.radioreference.com/threads/proscan-sds200-scanner-connection-dropping-on-internal-wifi.465284/page-2)
- **Repeated connect/disconnect also wedges it.** The Home Assistant integration keeps one
  audio session open for its whole lifetime to avoid this. Its auto-reboot watchdog holds
  off for the first 2.5 minutes after startup, so it doesn't power-cycle a scanner that is
  only declining a second session. It also supports a PoE reset.
  [immakinbacon/sds200ha](https://github.com/immakinbacon/sds200ha)
- **Only one audio client at a time.**
  [No audio through LAN port on SDS200](https://forums.radioreference.com/threads/no-audio-through-lan-port-on-sds200.398576/)
- **`MSM` as a remote reboot (undocumented).** ProScan's help suggests sending `MSM,0` in
  its protocol-command box to reboot the scanner without a smart plug; it needs a working
  control connection. ProScan's author says `MSM,0300` "definitely restores the RTSP audio",
  and showed it after deliberately crashing ProScan. Another user reports that after
  `MSM,0300` over UDP the scanner rebooted, but audio still didn't work until a power cycle.
  One summary describes `MSM,N` as "mass-storage mode for N seconds", after which the
  scanner restarts.
  [Uniden SDS200E audio stream hangs after MSM command](https://forums.radioreference.com/threads/uniden-sds200e-audio-stream-hangs-after-msm-command.500987/)
- **Uniden's documentation doesn't list `MSM`.** Neither command spec does (checked). The
  only power command is `POF` (power off), useless here because nothing can power the
  scanner back on remotely.
  [SDS Series Remote Command Specification V2.00 (2025)](https://info.uniden.com/twiki/pub/UnidenMan4/SDS100FirmwareUpdate/SDS_Series_RemoteCommand_Specification_V2_00.pdf),
  [SDS200 Remote Command Specification V0.01 (2018)](https://info.uniden.com/twiki/pub/UnidenMan4/SDS200FirmwareUpdate/SDS200_RemoteCommand_Specification_V0_01.pdf)
- **No firmware fix.** Uniden's changelog has no network, LAN or RTSP entries through Main
  1.26.01 (2026-03-27). The versions after our 1.21.00 are 1.23.01 through 1.26.01.
  [SDS200 Firmware Update Change Log](https://info.uniden.com/twiki/bin/view/UnidenMan4/SDS200FirmwareUpdate)

## What it means for this project

**Recovery works:** `MSM,0` (undocumented) reboots the scanner remotely over the UDP control
channel, which keeps working during the lockout. It clears the lockout on firmware 1.21.00,
and capture reconnects by itself about 20 s later.

**Prevention looks possible:** make sure the scanner eventually gets our FIN or TEARDOWN.

1. **Never `kill -9` capture,** and avoid anything that makes it connect and disconnect
   repeatedly.
2. **Keep the close alive longer (prevention, candidates):**
   - raise `net.ipv4.tcp_orphan_retries` (for example to 12, about 13 minutes of FIN
     retries) with a sysctl drop-in (system-wide, needs sudo, no code change);
   - or have capture hold the RTSP socket open, not orphaned, through an outage and send the
     TEARDOWN once the scanner answers on UDP again. Retries then follow `tcp_retries2 = 15`,
     about 15 min.

   Either would turn a 2-minute unplug into a short gap, and needs the same test repeated.
3. **Automatic recovery (safety net), like ProScan's:** if 554 is refused, and neither capture
   nor anything else holds it, for a few minutes while UDP control answers, and capture has
   been up for more than about 2.5 min, send `MSM,0`. Limit it to about one reboot per 15
   min, and record a system event. A smart plug is the backstop if the scanner stops
   answering UDP entirely.
4. Keeping the RTSP socket in systemd's file-descriptor store (the `kill -9` idea in
   `m5-plan.md` → Later) would only help with process deaths, not link breaks over 60 s.

## What we built (2026-09-30)

Both parts are in `source/sds200_network.py`, with tests in `tests/test_live_source.py`.

- **Prevention: parked connections.** `_ParkingRtspClient` replaces the library's
  `RtspClient`, through `NetworkAudioTransport(rtsp_client_factory=…)`. When a request fails
  mid-flight, it hands the socket to a background "parking lot" instead of closing it. The
  failed request can be a keepalive or a TEARDOWN, typically because the link is down. If the
  failed request wasn't the TEARDOWN, it first queues a TEARDOWN behind it. The kernel keeps
  retransmitting (`tcp_retries2`, about 15 min), so the TEARDOWN reaches the scanner when the
  link returns. The parked socket is closed when the scanner answers the TEARDOWN, closes or
  resets the connection, or after 20 min. It only parks when an RTSP session exists: a bare
  connection would just hold the scanner's single slot.
- **Recovery: automatic `MSM,0`.** In the reconnect loop, audio counts as "down" while the
  control channel answers but the audio start fails, or audio stops while status keeps
  coming. A dead link doesn't count. Capture reboots the scanner when all of these hold:
  - `auto_reboot` is on;
  - audio has been down for `auto_reboot_after_s` (180 s);
  - capture started at least `auto_reboot_grace_s` (150 s) ago;
  - the last reboot was at least `auto_reboot_min_interval_s` (900 s) ago;
  - no parked TEARDOWN is still pending.

  After the reboot it waits 20 s, then reconnects. After `auto_reboot_max_tries` (3) reboots
  without audio coming back, it stops and reports `needs_power_cycle`: a red status and an
  event. Settings are under `[scanner]` in `config.toml`.
- **Admin "Reboot scanner" button.** It asks for confirmation, then sends the `capture/reboot`
  command. Capture ends the session cleanly (TEARDOWN), sends `MSM,0` and reconnects. A
  manual reboot also resets the automatic give-up.
- **Follow-ups from the tests:** a connection closed without a live session is aborted
  with a TCP reset (`SO_LINGER` 0), because a normal close after a slow first reply left the
  scanner holding it half-open. Capture waits `PARK_SETTLE_S` (5 s) after a parked TEARDOWN
  is answered before connecting. RTSP requests allow 10 s (was 5).
- **Reconnect as soon as a parked TEARDOWN is answered** (`_sleep_until_retry`), rather than
  at the next scheduled retry (up to 60 s). This took about 40 s off the recovery.
- **Status:** the `rebooting` state is amber and `needs_power_cycle` is red. The events are
  `scanner_reboot` and `needs_power_cycle`. The "Empty: " error text is now the readable
  top-level message, for example "Timed out waiting for MDL response."
