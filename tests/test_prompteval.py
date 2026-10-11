"""Prompt test: labelled-clip discovery, scoring, worker runs it only while idle, Admin route."""
import json
from dataclasses import replace

import numpy as np
from test_worker import queue_clips, rows

from scanner_transcriber import commands, db, promptstore
from scanner_transcriber.audio import write_wav
from scanner_transcriber.config import Config, StorageConfig, TranscribeConfig
from scanner_transcriber.prompteval import edit_distance, labelled_clips
from scanner_transcriber.stt.base import SttResult
from scanner_transcriber.worker import Transcriber, run_worker


def make_session(d, refs):
    s = d / "sessions" / "20260930T000000Z"
    (s / "clips").mkdir(parents=True)
    with (s / "refs.jsonl").open("w") as f:
        for cid, text, skip in refs:
            write_wav(s / "clips" / f"{cid}.wav", np.ones(800, np.int16))
            f.write(json.dumps({"id": cid, "text": text, "skip": skip}) + "\n")


def test_labelled_clips_and_edit_distance(tmp_path):
    make_session(tmp_path, [("0001", "Medic 3 en route", False), ("0002", "mumble", True)])
    clips = labelled_clips(tmp_path)
    assert [c[0] for c in clips] == ["20260930T000000Z/0001"]
    assert edit_distance(["a", "b", "c"], ["a", "x", "c", "d"]) == 2


class PromptSensitive:
    """Fake Whisper: gets the word right only when the prompt contains it."""
    name = "ovwhisper"

    def __init__(self):
        self.prompt = None
        self.calls = []

    def load(self, c):
        self.prompt = c.prompt

    def transcribe(self, pcm, *, use_prompt=True):
        self.calls.append(self.prompt)
        return SttResult(text="I'm direct" if self.prompt and "direct" in self.prompt else "I'm Director")


class Silent:
    name = "sherpa"

    def load(self, c):
        pass

    def transcribe(self, pcm):
        return SttResult(text="")


def setup_eval(tmp_path, draft=None, version=None):
    conn = db.connect(tmp_path / "scanner.db")
    eid = conn.execute("INSERT INTO prompt_evals (prompt_version, draft_body, created_at, status) "
                       "VALUES (?, ?, 'now', 'queued')", (version, draft)).lastrowid
    commands.post(conn, "worker", "prompt-test", {"eval_id": eid})
    conn.commit()
    return conn, eid


def test_worker_runs_prompt_test_when_idle_and_restores_prompt(tmp_path):
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path),
                  stt=TranscribeConfig(prompt="Ashford."))
    make_session(tmp_path, [("0001", "I'm direct", False), ("0002", "I'm direct", False)])
    conn, eid = setup_eval(tmp_path, draft="Ashford. direct, I'm direct.")
    [live] = queue_clips(tmp_path, 1)                         # a live clip is waiting
    primary = PromptSensitive()
    t = Transcriber(cfg, primary=primary, fallback=Silent())
    t.load()
    run_worker(cfg, transcriber=t, drain=True)
    assert rows(tmp_path)[live]["transcript"] == "I'm Director"    # live clip used the active prompt
    ev = conn.execute("SELECT * FROM prompt_evals WHERE id = ?", (eid,)).fetchone()
    assert ev["status"] == "done" and ev["clips"] == 2 and ev["wer"] == 0.0
    assert primary.calls[0] == "Ashford."                    # live first, before the test
    assert primary.prompt == "Ashford."                      # active prompt restored afterwards
    assert "WER 0.000" in conn.execute("SELECT result FROM commands").fetchone()[0]


def test_prompt_test_of_a_saved_version_scores_errors(tmp_path):
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path),
                  stt=TranscribeConfig(prompt="Ashford."))
    make_session(tmp_path, [("0001", "I'm direct", False)])
    conn = db.connect(tmp_path / "scanner.db")
    promptstore.ensure_seeded(conn, cfg)
    v1 = promptstore.active(conn, "prompt")[0]
    conn.commit()
    conn, eid = setup_eval(tmp_path, version=v1)
    t = Transcriber(cfg, primary=PromptSensitive(), fallback=Silent())
    t.load()
    run_worker(cfg, transcriber=t, drain=True)
    ev = conn.execute("SELECT * FROM prompt_evals WHERE id = ?", (eid,)).fetchone()
    assert ev["status"] == "done" and ev["wer"] == 0.5         # "director" vs "direct": 1 of 2 words
    worst = json.loads(ev["detail"])["worst"]
    assert worst[0]["hyp"] == "I'm Director"


def test_no_labelled_clips_fails_cleanly(tmp_path):
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path))
    conn, eid = setup_eval(tmp_path, draft="x")
    t = Transcriber(cfg, primary=PromptSensitive(), fallback=Silent())
    t.load()
    run_worker(cfg, transcriber=t, drain=True)
    assert conn.execute("SELECT status FROM prompt_evals").fetchone()[0] == "failed"


def test_admin_prompt_test_route(admin_env):
    conn, client, d = admin_env
    make_session(d, [("0001", "clear", False)])
    html = client.get("/admin").text
    assert 'id="evals"' in html and "1 hand-labelled clips" in html
    r = client.post("/admin/prompt-test", headers={"HX-Request": "true"})
    assert "waiting for the worker to be idle" in r.text and 'hx-trigger="every 5s"' in r.text
    r = client.post("/admin/prompt-test", data={"draft": "a  draft  prompt"}, headers={"HX-Request": "true"})
    assert conn.execute("SELECT draft_body FROM prompt_evals ORDER BY id DESC").fetchone()[0] == "a draft prompt"
    assert conn.execute("SELECT COUNT(*) FROM commands WHERE action='prompt-test'").fetchone()[0] == 2


def test_a_test_interrupted_by_a_restart_is_marked_failed_on_start(tmp_path):
    cfg = replace(Config(), storage=StorageConfig(data_dir=tmp_path))
    conn, eid = setup_eval(tmp_path, draft="x")
    commands.take(conn, "worker", {"prompt-test"})             # the old worker had taken it...
    conn.execute("UPDATE prompt_evals SET status = 'running', clips = 3")  # ...and died mid-test
    conn.commit()
    t = Transcriber(cfg, primary=PromptSensitive(), fallback=Silent())
    t.load()
    run_worker(cfg, transcriber=t, drain=True)
    ev = conn.execute("SELECT * FROM prompt_evals WHERE id = ?", (eid,)).fetchone()
    assert ev["status"] == "failed" and "restarted" in json.loads(ev["detail"])["error"]
    cmd = conn.execute("SELECT done_at, result FROM commands").fetchone()
    assert cmd["done_at"] and "interrupted" in cmd["result"]


def test_a_queued_test_nobody_picked_up_is_marked_failed(tmp_path):
    from scanner_transcriber import prompteval
    conn, eid = setup_eval(tmp_path, draft="x")
    conn.execute("UPDATE commands SET created_at = '2026-01-01T00:00:00+00:00'")  # long ago
    commands.take(conn, "capture")                              # any poll expires stale commands
    assert prompteval.fail_expired(conn) == 1
    ev = conn.execute("SELECT * FROM prompt_evals WHERE id = ?", (eid,)).fetchone()
    assert ev["status"] == "failed" and "no worker" in json.loads(ev["detail"])["error"]


def test_admin_delete_prompt_test(admin_env):
    conn, client, d = admin_env
    conn.execute("INSERT INTO prompt_evals (created_at, status, wer, cer, clips) VALUES ('2026-09-30', 'done', 0.3, 0.2, 72)")
    eid = conn.execute("SELECT id FROM prompt_evals").fetchone()[0]
    assert "Delete" in client.get("/admin").text
    r = client.post(f"/admin/prompt-test/{eid}/delete", headers={"HX-Request": "true"})
    assert r.status_code == 200 and conn.execute("SELECT COUNT(*) FROM prompt_evals").fetchone()[0] == 0
    assert client.post(f"/admin/prompt-test/{eid}/delete").status_code == 403       # guard
