"""Score the real pipeline against hand labels: for each labelled clip of a session, find the
DB rows that overlap it in time (a replay of that session), join their transcripts, and
compute WER. Clip IDs don't need to match, so it works after segmenter changes."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .. import db
from ..bench import normalize
from ..config import Config
from ..timeline import AnchorClock


def _wall0(session_dir: Path) -> datetime:
    """Wall time of sample 0, exactly as ReplaySource computes it."""
    s = json.loads((session_dir / "session.json").read_text())
    anchors = [json.loads(line) for line in (session_dir / "anchors.jsonl").open()]
    clock = AnchorClock([(a["mono"], a["sample"]) for a in anchors])
    return datetime.fromisoformat(s["started_wall"]) + timedelta(
        seconds=clock.to_mono(0) - s["started_mono"])


def run(cfg: Config, session_dir: Path, *, show: int = 10) -> dict:
    import jiwer

    wall0 = _wall0(session_dir)
    clips = {c["id"]: c for c in map(json.loads, (session_dir / "clips.jsonl").open())}
    refs = [r for r in map(json.loads, (session_dir / "refs.jsonl").open()) if not r["skip"]]
    conn = db.connect(cfg.storage.db_path)
    rows = [dict(r) for r in conn.execute(
        "SELECT id, started_at, ended_at, status, transcript, flags FROM transmissions")]
    conn.close()
    for r in rows:
        r["t0"] = (datetime.fromisoformat(r["started_at"]) - wall0).total_seconds()
        r["t1"] = (datetime.fromisoformat(r["ended_at"]) - wall0).total_seconds()

    pairs, missing, unfinished = [], [], 0
    for ref in refs:
        c = clips[ref["id"]]
        a = c["start_s"]
        b = a + c["duration_ms"] / 1000
        hits = sorted((r for r in rows if min(b, r["t1"]) - max(a, r["t0"]) > 0.2),
                      key=lambda r: r["t0"])
        if not hits:
            missing.append(ref["id"])
            continue
        unfinished += sum(1 for h in hits if h["status"] != "done")
        hyp = " ".join((h["transcript"] or "") for h in hits).strip()
        pairs.append((ref["id"], ref["text"], hyp, len(hits)))

    R = [normalize(p[1]) or "<empty>" for p in pairs]
    H = [normalize(p[2]) or "<empty>" for p in pairs]
    report = {
        "session": str(session_dir), "labelled": len(refs), "matched": len(pairs),
        "missing": missing, "not_done": unfinished,
        "split_by_segmenter": sum(1 for p in pairs if p[3] > 1),
        "wer": round(jiwer.wer(R, H), 3) if pairs else None,
        "cer": round(jiwer.cer(R, H), 3) if pairs else None,
    }
    worst = sorted(pairs, key=lambda p: -jiwer.wer(normalize(p[1]) or "x", normalize(p[2]) or "<e>"))
    report["worst"] = [{"id": p[0], "ref": p[1], "hyp": p[2]} for p in worst[:show]]
    return report


def print_report(rep: dict) -> None:
    print(f"{rep['matched']}/{rep['labelled']} labelled clips matched "
          f"({rep['split_by_segmenter']} split into several rows, {rep['not_done']} rows not done)")
    if rep["missing"]:
        print(f"no matching row for: {', '.join(rep['missing'])}")
    print(f"WER {rep['wer']}  CER {rep['cer']}")
    for w in rep["worst"]:
        print(f"  {w['id']}  ref: {w['ref']}\n        hyp: {w['hyp']}")
