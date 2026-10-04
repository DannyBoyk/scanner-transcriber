"""Prompt test: score a prompt against the hand-labelled clips (data/sessions/*/refs.jsonl).

Run by the worker one clip at a time, and only while no live clip is waiting, so it never
delays live transcription. Uses the full transcription path (guards + substitutions), so the
score reflects what users would actually see."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .audio import read_wav, resample
from .bench import normalize
from .db import utcnow

log = logging.getLogger(__name__)


def labelled_clips(data_dir: Path) -> list[tuple[str, str, Path]]:
    """[(clip key, reference text, wav path)] for every labelled, non-skipped clip."""
    out = []
    for refs in sorted(Path(data_dir, "sessions").glob("*/refs.jsonl")):
        session = refs.parent
        for line in refs.open():
            r = json.loads(line)
            wav = session / "clips" / f"{r['id']}.wav"
            if not r.get("skip") and wav.exists():
                out.append((f"{session.name}/{r['id']}", r.get("text", ""), wav))
    return out


def edit_distance(a: list[str], b: list[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, y in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y))
        prev = cur
    return prev[-1]


@dataclass
class EvalRun:
    eval_id: int
    command: dict
    prompt: str
    clips: list
    i: int = 0
    word_err: int = 0
    word_ref: int = 0
    char_err: int = 0
    char_ref: int = 0
    per_clip: list = field(default_factory=list)

    @property
    def finished(self) -> bool:
        return self.i >= len(self.clips)

    def step(self, transcriber, active_prompt: str) -> None:
        key, ref, wav = self.clips[self.i]
        self.i += 1
        pcm, rate = read_wav(wav)
        transcriber.set_prompt(self.prompt)
        try:
            hyp = transcriber.transcribe(resample(pcm, rate)).text
        finally:
            transcriber.set_prompt(active_prompt)       # live clips keep the active prompt
        r, h = normalize(ref).split(), normalize(hyp).split()
        we = edit_distance(r, h)
        rc, hc = " ".join(r), " ".join(h)
        self.word_err += we
        self.word_ref += max(len(r), 1)
        self.char_err += edit_distance(list(rc), list(hc))
        self.char_ref += max(len(rc), 1)
        self.per_clip.append({"clip": key, "ref": ref, "hyp": hyp,
                              "wer": round(we / max(len(r), 1), 3)})

    def progress(self, conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE prompt_evals SET status = 'running', clips = ? WHERE id = ?",
                     (self.i, self.eval_id))

    def finish(self, conn: sqlite3.Connection) -> tuple[float, float]:
        wer = self.word_err / max(self.word_ref, 1)
        cer = self.char_err / max(self.char_ref, 1)
        worst = sorted(self.per_clip, key=lambda c: -c["wer"])[:10]
        conn.execute("UPDATE prompt_evals SET status = 'done', clips = ?, wer = ?, cer = ?, "
                     "detail = ? WHERE id = ?",
                     (len(self.per_clip), round(wer, 4), round(cer, 4),
                      json.dumps({"worst": worst}), self.eval_id))
        return wer, cer


def start(conn: sqlite3.Connection, command: dict, data_dir: Path) -> EvalRun | None:
    """Prepare the run for a `worker/prompt-test` command. None if there's nothing to do."""
    from . import commands, promptstore

    eval_id = command["arg"].get("eval_id")
    row = conn.execute("SELECT * FROM prompt_evals WHERE id = ?", (eval_id,)).fetchone()
    if row is None:
        commands.done(conn, command, "prompt test not found", "warn")
        return None
    if row["draft_body"]:
        prompt = row["draft_body"]
    else:
        body = conn.execute("SELECT body FROM prompt_versions WHERE id = ?",
                            (row["prompt_version"],)).fetchone()
        prompt = body[0] if body else promptstore.active(conn, "prompt")[1]
    clips = labelled_clips(data_dir)
    if not clips:
        conn.execute("UPDATE prompt_evals SET status = 'failed', detail = ? WHERE id = ?",
                     (json.dumps({"error": "no hand-labelled clips found"}), eval_id))
        commands.done(conn, command, "no hand-labelled clips found", "warn")
        return None
    conn.execute("UPDATE prompt_evals SET status = 'running', clips = 0 WHERE id = ?", (eval_id,))
    log.info("prompt test %s: %d labelled clips", eval_id, len(clips))
    return EvalRun(eval_id=eval_id, command=command, prompt=prompt, clips=clips)


INTERRUPTED = "interrupted: the worker restarted"
EXPIRED = "no worker picked it up (was transcription running?)"


def recover_interrupted(conn: sqlite3.Connection) -> int:
    """At worker start: a test still 'running' was cut off by a restart (one worker only)."""
    n = conn.execute("UPDATE prompt_evals SET status = 'failed', detail = ? "
                     "WHERE status = 'running'", (json.dumps({"error": INTERRUPTED}),)).rowcount
    conn.execute("UPDATE commands SET done_at = ?, result = ? WHERE action = 'prompt-test' "
                 "AND taken_at IS NOT NULL AND done_at IS NULL", (utcnow(), INTERRUPTED))
    return n


def fail_expired(conn: sqlite3.Connection) -> int:
    """A queued test whose command expired unclaimed would otherwise wait forever."""
    return conn.execute(
        "UPDATE prompt_evals SET status = 'failed', detail = ? WHERE status = 'queued' AND id IN "
        "(SELECT json_extract(arg, '$.eval_id') FROM commands "
        " WHERE action = 'prompt-test' AND done_at IS NOT NULL)",
        (json.dumps({"error": EXPIRED}),)).rowcount
