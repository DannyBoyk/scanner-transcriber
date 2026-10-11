"""Build a labelling session from recent recordings (for `draft` + `label`, then prompt tests).

Picks a deliberate mix from the database, because a plain random sample under-represents the
hard cases: short replies ("Direct", "Copy"), transmissions that open with a unit ID, and calls
naming a local place the model struggles with (`--place`, e.g. your county's name). Copies the clips (8 kHz WAV, as recorded) into
data/sessions/<name>/clips/NNNN.wav and writes clips.jsonl in the same layout `cut` produces,
so prompt tests pick the session up automatically once refs.jsonl exists."""
from __future__ import annotations

import json
import random
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from ..audio import read_wav, rms_dbfs

SHORT_MS = 1500
UNIT_OPENER = r"^\s*(\d|one|two|three|four|five|six|seven|eight|nine|medic|engine|ambulance|tanker|battalion)"


def pick(rows: list[sqlite3.Row], n: int, seed: int = 1,
         places: tuple[str, ...] = ()) -> list[sqlite3.Row]:
    """About 35% short replies, 35% unit-ID openers / calls naming one of `places`, the rest anything else;
    spread over time within each group. Fills from the other groups if one runs short."""
    rng = random.Random(seed)
    short = [r for r in rows if r["duration_ms"] <= SHORT_MS]
    unit_or_place = re.compile("|".join([UNIT_OPENER] + [re.escape(p) for p in places]), re.IGNORECASE)
    units = [r for r in rows if r["duration_ms"] > SHORT_MS and unit_or_place.search(r["transcript"] or "")]
    taken = {r["id"] for r in short + units}
    rest = [r for r in rows if r["id"] not in taken]
    want = {"short": round(n * 0.35), "units": round(n * 0.35)}
    want["rest"] = n - want["short"] - want["units"]
    out: list[sqlite3.Row] = []
    for name, group in (("short", short), ("units", units), ("rest", rest)):
        rng.shuffle(group)
        out += group[:want[name]]
    if len(out) < n:                                   # a group ran short: top up from the others
        spare = [r for r in short + units + rest if r["id"] not in {x["id"] for x in out}]
        rng.shuffle(spare)
        out += spare[:n - len(out)]
    return sorted(out, key=lambda r: r["started_at"])


def run(db_path: Path, data_dir: Path, *, n: int = 100, name: str | None = None,
        seed: int = 1, places: tuple[str, ...] = ()) -> Path:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, started_at, duration_ms, system, department, channel, tgid, unit_ids,
                  audio_path, transcript FROM transmissions
           WHERE status = 'done' AND audio_path IS NOT NULL AND duration_ms <= 15000
           ORDER BY id""").fetchall()
    conn.close()
    rows = [r for r in rows if (data_dir / r["audio_path"]).exists()]
    chosen = pick(rows, n, seed, places)
    name = name or f"{datetime.now():%Y%m%d}-recent"
    out = data_dir / "sessions" / name
    if out.exists():
        raise SystemExit(f"{out} already exists; pass --name for a new set")
    (out / "clips").mkdir(parents=True)
    with (out / "clips.jsonl").open("w") as f:
        for i, r in enumerate(chosen):
            cid = f"{i:04d}"
            shutil.copy2(data_dir / r["audio_path"], out / "clips" / f"{cid}.wav")
            pcm, _ = read_wav(out / "clips" / f"{cid}.wav")
            f.write(json.dumps({
                "id": cid, "start_s": 0.0, "duration_ms": r["duration_ms"],
                "rms_dbfs": round(rms_dbfs(pcm[pcm != 0]) if pcm.any() else -120.0, 1),
                "system": r["system"], "department": r["department"], "channel": r["channel"],
                "tgid": r["tgid"], "units": json.loads(r["unit_ids"] or "[]"),
                "continued": False, "status_missing": False,
                "tx_id": r["id"], "started_at": r["started_at"]}) + "\n")
    (out / "session.json").write_text(json.dumps({
        "source": "database sample (tools/sample.py)", "created": datetime.now().isoformat(),
        "clips": len(chosen), "seed": seed,
        "mix": {"short <=1.5 s": sum(1 for r in chosen if r["duration_ms"] <= SHORT_MS)}}, indent=1))
    return out
