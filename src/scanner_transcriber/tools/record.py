"""Record a live session (audio + status) in ReplaySource format.

Output directory data/sessions/<UTC ts>/:
  audio.wav      8 kHz mono int16, laid out by RTP timestamp (gaps zero-filled)
  anchors.jsonl  ~1/s {"mono", "sample"}: monotonic arrival time of a given sample index
  status.jsonl   every PSI push: {"mono", "wall", "status": {...}, "xml"}
  session.json   start/stop info, RTP statistics, teardown confirmation
"""
from __future__ import annotations

import atexit
import json
import logging
import signal
import threading
import time
import wave
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from sds200 import SDSScanner
from sds200.models import ScannerInfo
from sds200.network_audio import NetworkAudioTransport

from ..audio import SCANNER_RATE, ulaw_to_pcm16
from ..config import Config
from ..source.sds200_network import AudioSessionLock, summarize_status
from ..timeline import RtpTimeline

log = logging.getLogger(__name__)

class _WavTimeline:
    """Writes PCMU payloads into a WAV, positioned by RTP timestamp."""

    def __init__(self, path: Path, anchors_path: Path) -> None:
        self._wav = wave.open(str(path), "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(SCANNER_RATE)
        self._anchors = anchors_path.open("w")
        self._lock = threading.Lock()
        self._timeline = RtpTimeline()
        self._last_anchor_mono = 0.0

    @property
    def samples_written(self) -> int:
        return self._timeline.samples

    @property
    def zero_filled(self) -> int:
        return self._timeline.zero_filled

    @property
    def resets(self) -> int:
        return self._timeline.resets

    def on_packet(self, pkt) -> None:
        mono = time.monotonic()
        with self._lock:
            if self._wav is None:
                return
            start, pcm = self._timeline.place(pkt.timestamp, ulaw_to_pcm16(pkt.payload))
            if pcm.size:
                self._wav.writeframes(pcm.astype("<i2").tobytes())
            if mono - self._last_anchor_mono >= 1.0:
                # anchor = arrival time of this packet's first sample
                pos = self._timeline.samples - len(pkt.payload)
                self._anchors.write(json.dumps({"mono": mono, "sample": pos}) + "\n")
                self._anchors.flush()
                self._last_anchor_mono = mono

    def close(self) -> None:
        with self._lock:
            if self._wav is not None:
                self._wav.close()
                self._wav = None
            self._anchors.close()


def run(cfg: Config, *, duration_s: float | None) -> Path:
    data_dir = cfg.storage.data_dir
    out_dir = data_dir / "sessions" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    session: dict = {
        "scanner_id": cfg.scanner.id,
        "host": cfg.scanner.host,
        "started_wall": datetime.now(UTC).isoformat(),
        "started_mono": time.monotonic(),
    }
    stop = threading.Event()

    def _signal(signum, _frame):
        log.info("signal %s: stopping", signum)
        stop.set()

    old_handlers = {s: signal.signal(s, _signal) for s in (signal.SIGINT, signal.SIGTERM)}

    with AudioSessionLock(data_dir / ".rtsp.lock"):
        timeline = _WavTimeline(out_dir / "audio.wav", out_dir / "anchors.jsonl")
        status_f = (out_dir / "status.jsonl").open("w")
        status_lock = threading.Lock()
        status_count = 0

        def on_psi(msg) -> None:
            nonlocal status_count
            if not isinstance(msg, ScannerInfo):
                return  # PSI ack packets
            line = json.dumps({"mono": time.monotonic(), "wall": msg.received_at.isoformat(),
                               "status": summarize_status(msg), "xml": msg.raw_xml})
            with status_lock:
                if not status_f.closed:
                    status_f.write(line + "\n")
                    status_count += 1

        audio = NetworkAudioTransport(cfg.scanner.host, rtsp_port=cfg.scanner.rtsp_port)
        audio.on_packet(timeline.on_packet)
        scanner = SDSScanner.network(cfg.scanner.host, remote_port=cfg.scanner.control_port)

        torn_down = False

        def teardown() -> None:
            nonlocal torn_down
            if torn_down:
                return
            torn_down = True
            audio.stop()  # sends RTSP TEARDOWN
            log.info("RTSP teardown sent (teardowns_sent=%d)", audio.statistics.teardowns_sent)

        atexit.register(teardown)
        try:
            scanner.connect()
            session["model"] = str(scanner.get_model())
            session["firmware"] = scanner.get_firmware()
            scanner.on_psi(on_psi)
            scanner.start_scanner_info_push(cfg.scanner.psi_interval_ms)

            audio.start(lambda _chunk: None)
            log.info("recording to %s (%s)", out_dir,
                     f"{duration_s:.0f}s" if duration_s else "until Ctrl-C")
            deadline = time.monotonic() + duration_s if duration_s else None
            last_report = time.monotonic()
            last_samples = -1
            while not stop.is_set():
                if deadline and time.monotonic() >= deadline:
                    break
                stop.wait(0.5)
                now = time.monotonic()
                if now - last_report >= 30:
                    st = audio.statistics
                    log.info("audio %.0fs (zero-filled %.0fs) packets=%d lost=%d status=%d",
                             timeline.samples_written / SCANNER_RATE,
                             timeline.zero_filled / SCANNER_RATE,
                             st.packets_delivered, st.packets_lost, status_count)
                    if timeline.samples_written == last_samples:
                        log.warning("no RTP audio in the last 30s")
                    last_samples = timeline.samples_written
                    last_report = now
        finally:
            teardown()
            atexit.unregister(teardown)
            try:
                scanner.stop_scanner_info_push()
            except Exception:
                log.exception("stopping PSI failed")
            scanner.close()
            timeline.close()
            with status_lock:
                status_f.close()
            for s, h in old_handlers.items():
                signal.signal(s, h)

            stats = asdict(audio.statistics)
            session.update({
                "stopped_wall": datetime.now(UTC).isoformat(),
                "stopped_mono": time.monotonic(),
                "audio_seconds": timeline.samples_written / SCANNER_RATE,
                "zero_filled_seconds": timeline.zero_filled / SCANNER_RATE,
                "rtp_timestamp_resets": timeline.resets,
                "status_messages": status_count,
                "rtp": stats,
            })
            (out_dir / "session.json").write_text(json.dumps(session, indent=2, default=str))

    log.info("done: %.1fs audio, %d status msgs, teardowns_sent=%d → %s",
             session["audio_seconds"], status_count, stats["teardowns_sent"], out_dir)
    return out_dir


def peak_dbfs(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    if pcm.size == 0:
        return -120.0
    return float(20 * np.log10(max(np.abs(pcm.astype(np.int32)).max(), 1) / 32768))
