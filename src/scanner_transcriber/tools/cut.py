"""Cut a recorded session into per-transmission clips.

`run_segmenter` uses the production M1 Segmenter (the default for `cut`). `run` below is the
original M0 audio-only cutter, kept because the M0 labelled session's clip IDs came from it.

M0 cutter: SDS200 network audio is exact digital silence while muted, so a transmission is a
run of non-silent 20 ms frames; runs closer than `merge_gap_ms` are joined. Labels
come from the PSI status stream, shifted by `latency_ms` into the audio timebase.
Also measures how status Unmute lines up with audio onset (open questions 2/3).
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

import numpy as np

from ..audio import SCANNER_RATE, read_wav, rms_dbfs, write_wav

FRAME = SCANNER_RATE // 50  # 20 ms


def _active_frames(pcm: np.ndarray, threshold: int) -> np.ndarray:
    n = len(pcm) // FRAME
    frames = np.abs(pcm[: n * FRAME].astype(np.int32)).reshape(n, FRAME)
    return frames.max(axis=1) > threshold


def _runs(active: np.ndarray, merge_gap_frames: int) -> list[tuple[int, int]]:
    runs: list[list[int]] = []
    idx = np.flatnonzero(active)
    for i in idx:
        if runs and i - runs[-1][1] <= merge_gap_frames:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1])
    return [(a, b) for a, b in runs]


class _Clock:
    """Maps monotonic time to sample index using the recorder's anchors."""

    def __init__(self, anchors: list[dict]) -> None:
        self.mono = np.array([a["mono"] for a in anchors])
        self.sample = np.array([a["sample"] for a in anchors], dtype=np.float64)

    def to_sample(self, mono: float) -> float:
        i = int(np.clip(np.searchsorted(self.mono, mono) - 1, 0, len(self.mono) - 1))
        return self.sample[i] + (mono - self.mono[i]) * SCANNER_RATE


def run_segmenter(session_dir: Path) -> Path:
    """Cut with the production Segmenter (status-driven, one clip per PTT)."""
    import asyncio

    from ..config import SegmenterConfig
    from ..models import AudioFrame
    from ..segmenter import Segmenter
    from ..source.replay import ReplaySource

    async def collect():
        seg, clips = Segmenter(SegmenterConfig()), []
        async for ev in ReplaySource(session_dir).events():
            clips += seg.feed_audio(ev) if isinstance(ev, AudioFrame) else seg.feed_status(ev)
        return clips + seg.flush()

    clips = asyncio.run(collect())
    out_dir = session_dir / "clips"
    out_dir.mkdir(exist_ok=True)
    rows = []
    for i, c in enumerate(clips):
        cid = f"{i:04d}"
        write_wav(out_dir / f"{cid}.wav", c.pcm)
        st = c.status
        rows.append({
            "id": cid, "start_s": round(c.start_sample / SCANNER_RATE, 3),
            "duration_ms": c.duration_ms, "rms_dbfs": round(rms_dbfs(c.pcm), 1),
            "system": st.system if st else None, "department": st.department if st else None,
            "channel": st.channel if st else None, "tgid": st.tgid if st else None,
            "units": c.unit_ids, "continued": c.continued, "status_missing": c.status_missing,
        })
    with (session_dir / "clips.jsonl").open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    durations = [r["duration_ms"] / 1000 for r in rows]
    report = {
        "session": str(session_dir), "method": "segmenter", "clips": len(rows),
        "active_s": round(sum(durations), 1),
        "clip_s_median": round(statistics.median(durations), 2) if durations else None,
        "clip_s_max": round(max(durations), 2) if durations else None,
        "clips_without_tgid": sum(1 for r in rows if not r["tgid"]),
        "status_missing": sum(r["status_missing"] for r in rows),
        "talkgroups": Counter(f'{r["tgid"]} {r["department"]}/{r["channel"]}'
                              for r in rows).most_common(),
    }
    (session_dir / "cut_report.json").write_text(json.dumps(report, indent=2))
    return session_dir / "cut_report.json"


def run(session_dir: Path, *, merge_gap_ms: int = 400, min_clip_ms: int = 400,
        pad_ms: int = 100, latency_ms: int = 0, threshold: int = 8) -> Path:
    pcm, rate = read_wav(session_dir / "audio.wav")
    assert rate == SCANNER_RATE
    anchors = [json.loads(line) for line in (session_dir / "anchors.jsonl").open()]
    status = [json.loads(line) for line in (session_dir / "status.jsonl").open()]
    clock = _Clock(anchors)
    for s in status:
        s["t"] = clock.to_sample(s["mono"]) / SCANNER_RATE - latency_ms / 1000

    active = _active_frames(pcm, threshold)
    runs = _runs(active, merge_gap_ms // 20)
    out_dir = session_dir / "clips"
    out_dir.mkdir(exist_ok=True)
    pad = pad_ms * SCANNER_RATE // 1000

    clips, onset_offsets, dropped = [], [], 0
    status_t = np.array([s["t"] for s in status])
    for a, b in runs:
        start, end = a * FRAME, b * FRAME
        dur_ms = (end - start) * 1000 // SCANNER_RATE
        if dur_ms < min_clip_ms:
            dropped += 1
            continue
        t0, t1 = start / SCANNER_RATE, end / SCANNER_RATE
        lo, hi = np.searchsorted(status_t, t0 - 1.0), np.searchsorted(status_t, t1)
        window = status[lo:hi]
        tgids = Counter(s["status"]["tgid"] for s in window if s["status"]["tgid"])
        tgid = tgids.most_common(1)[0][0] if tgids else None
        labelled = [s["status"] for s in window if s["status"]["tgid"] == tgid] or [{}]
        meta = labelled[0]
        units = sorted({s.get("unit_id") for s in labelled if s.get("unit_id")})
        unmutes = [s["t"] - t0 for s in window if s["status"]["mute"] == "Unmute"]
        if unmutes:
            onset_offsets.append(unmutes[0])

        cid = f"{len(clips):04d}"
        clip_pcm = pcm[max(0, start - pad): end + pad]
        write_wav(out_dir / f"{cid}.wav", clip_pcm)
        clips.append({
            "id": cid, "start_s": round(t0, 3), "duration_ms": int(dur_ms),
            "rms_dbfs": round(rms_dbfs(pcm[start:end]), 1),
            "system": meta.get("system"), "department": meta.get("department"),
            "channel": meta.get("channel"), "tgid": tgid, "units": units,
            "tgid_changes": len(tgids) > 1,
        })

    with (session_dir / "clips.jsonl").open("w") as f:
        for c in clips:
            f.write(json.dumps(c) + "\n")

    durations = [c["duration_ms"] / 1000 for c in clips]
    report = {
        "session": str(session_dir),
        "audio_s": len(pcm) / SCANNER_RATE,
        "active_s": round(sum(durations), 1),
        "clips": len(clips),
        "dropped_short": dropped,
        "clip_s_median": round(statistics.median(durations), 2) if durations else None,
        "clip_s_max": round(max(durations), 2) if durations else None,
        "clips_without_tgid": sum(1 for c in clips if not c["tgid"]),
        "clips_spanning_tgid_change": sum(1 for c in clips if c["tgid_changes"]),
        # status Unmute time minus audio onset; positive = metadata lags audio
        "unmute_minus_onset_ms_median": (
            round(statistics.median(onset_offsets) * 1000) if onset_offsets else None),
        "unmute_minus_onset_ms_p10_p90": (
            [round(float(np.percentile(onset_offsets, p)) * 1000) for p in (10, 90)]
            if len(onset_offsets) >= 5 else None),
        "clips_without_unmute_status": len(clips) - len(onset_offsets),
        "talkgroups": Counter(f'{c["tgid"]} {c["department"]}/{c["channel"]}'
                              for c in clips).most_common(),
    }
    (session_dir / "cut_report.json").write_text(json.dumps(report, indent=2))
    return session_dir / "cut_report.json"
