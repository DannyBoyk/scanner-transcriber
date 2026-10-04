"""Draft reference transcripts with a large, slow model, for a person to correct.

Picks up to N clips spread across the session (preferring 1.5-15 s clips and a mix of
talkgroups), transcribes them with beam search, and writes <session>/refs_draft.jsonl.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from ..audio import read_wav, resample
from ..stt.base import SttConfig, get_backend


def select_clips(clips: list[dict], n: int, min_ms: int = 1500) -> list[dict]:
    good = [c for c in clips if min_ms <= c["duration_ms"] <= 15000] or clips
    if len(good) <= n:
        return good
    step = len(good) / n
    return [good[int(i * step)] for i in range(n)]


def run(session_dir: Path, *, n: int = 50, backend: str = "fasterwhisper",
        model: str = "large-v3-turbo", threads: int = 4, prompt: str | None = None,
        extra: dict | None = None, min_ms: int = 1500) -> Path:
    clips = [json.loads(line) for line in (session_dir / "clips.jsonl").open()]
    chosen = select_clips(clips, n, min_ms)
    b = get_backend(backend)
    b.load(SttConfig(model=model, threads=threads, prompt=prompt,
                     extra={"beam_size": 5, **(extra or {})}))
    out = session_dir / "refs_draft.jsonl"
    done = set()
    if out.exists():
        done = {json.loads(line)["id"] for line in out.open()}
    with out.open("a") as f:
        for i, c in enumerate(chosen, 1):
            if c["id"] in done:
                continue
            pcm, rate = read_wav(session_dir / "clips" / f"{c['id']}.wav")
            t = time.perf_counter()
            r = b.transcribe(resample(pcm, rate))
            el = time.perf_counter() - t
            f.write(json.dumps({"id": c["id"], "draft": r.text, "model": f"{backend}:{model}",
                                "confidence": r.confidence}) + "\n")
            f.flush()
            print(f"[{i}/{len(chosen)}] {c['id']} {len(pcm) / rate:.1f}s in {el:.1f}s: {r.text}",
                  flush=True)
    return out
