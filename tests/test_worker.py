"""Worker logic with fake STT backends (no models, no GPU)."""
import threading
from dataclasses import replace

import numpy as np
import pytest

from scanner_transcriber import db
from scanner_transcriber.clipstore import ClipStore
from scanner_transcriber.config import Config, StorageConfig, TranscribeConfig
from scanner_transcriber.stt.base import SttResult
from scanner_transcriber.worker import Transcriber, run_worker

from test_db import clip

PROMPT = "Northfield, Ashford, Brookfield. Alpha, Bravo, Charlie, Delta, Echo."
X = np.zeros(16000, np.float32)


class Fake:
    """Returns scripted outputs; with_prompt/without_prompt distinguish the leak retry."""

    def __init__(self, name, with_prompt="", without_prompt=None, fail_load=False, boom=False):
        self.name, self.with_prompt = name, with_prompt
        self.without_prompt = with_prompt if without_prompt is None else without_prompt
        self.fail_load, self.boom, self.calls = fail_load, boom, []

    def load(self, cfg):
        if self.fail_load:
            raise RuntimeError("GPU device not found")

    def transcribe(self, pcm, *, use_prompt=True):
        self.calls.append(use_prompt)
        if self.boom:
            raise ValueError("decoder exploded")
        return SttResult(text=self.with_prompt if use_prompt else self.without_prompt)


def cfg_for(tmp_path, **stt):
    return replace(Config(), storage=StorageConfig(data_dir=tmp_path),
                   stt=TranscribeConfig(prompt=PROMPT, **stt))


def make(tmp_path, primary, fallback=None, **stt):
    t = Transcriber(cfg_for(tmp_path, **stt), primary=primary,
                    fallback=fallback or Fake("sherpa", ""))
    t.load()
    return t


def test_plain_transcript(tmp_path):
    r = make(tmp_path, Fake("ovwhisper", "Medic 3 en route.")).transcribe(X)
    assert (r.text, r.flags, r.no_speech, r.backend) == ("Medic 3 en route.", [], False,
                                                         "ovwhisper")


def test_blank_output_is_no_speech(tmp_path):
    r = make(tmp_path, Fake("ovwhisper", "... ...")).transcribe(X)
    assert r.no_speech and r.text == "" and r.raw == "... ..."


def test_hallucination_blanked_when_parakeet_hears_nothing(tmp_path):
    r = make(tmp_path, Fake("ovwhisper", "Thank you."), Fake("sherpa", "")).transcribe(X)
    assert r.no_speech and r.text == "" and r.flags == ["hallucination"]


def test_hallucination_kept_when_parakeet_hears_speech(tmp_path):
    r = make(tmp_path, Fake("ovwhisper", "Thank you."),
             Fake("sherpa", "thank you medic three")).transcribe(X)
    assert not r.no_speech and r.text == "Thank you."
    assert r.flags == ["hallucination"] and r.alt == "thank you medic three"


def test_prompt_leak_retried_without_prompt(tmp_path):
    p = Fake("ovwhisper", "Uniform Alpha Bravo Charlie Delta", without_prompt="Uniform foxtrot")
    r = make(tmp_path, p).transcribe(X)
    assert r.text == "Uniform foxtrot" and "prompt_leak" in r.flags and p.calls == [True, False]
    assert r.raw == "Uniform Alpha Bravo Charlie Delta"


def test_prompt_leak_keeps_original_if_retry_is_worse(tmp_path):
    p = Fake("ovwhisper", "Northfield Ashford Brookfield Alpha", without_prompt="Thank you.")
    r = make(tmp_path, p).transcribe(X)
    assert r.text == "Northfield Ashford Brookfield Alpha" and r.flags == ["prompt_leak"]


def test_repeats_collapsed_and_substitutions_applied(tmp_path):
    r = make(tmp_path, Fake("ovwhisper", "Charley 5802, Charley 5802, Charley 5802"),
             substitutions={"Charley": "Charlie"}).transcribe(X)
    assert r.text == "Charlie 5802," and r.flags == ["repeat"]


def test_gpu_failure_switches_to_degraded_mode(tmp_path):
    t = make(tmp_path, Fake("ovwhisper", fail_load=True), Fake("sherpa", "engine twelve"))
    assert t.degraded and "GPU device not found" in t.degraded_reason and t.mode == "degraded"
    r = t.transcribe(X)
    assert (r.text, r.flags, r.backend) == ("engine twelve", ["degraded"], "sherpa")


# ---- run_worker against a real DB ----------------------------------------------------------

def queue_clips(tmp_path, n):
    conn = db.connect(tmp_path / "scanner.db")
    store = ClipStore(conn, tmp_path, "sds200-1")
    ids = [store.save(clip(t)) for t in range(n)]
    conn.close()
    return ids


def rows(tmp_path):
    conn = db.connect(tmp_path / "scanner.db")
    out = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM transmissions")}
    conn.close()
    return out


def test_run_worker_drains_queue(tmp_path):
    ids = queue_clips(tmp_path, 3)
    t = make(tmp_path, Fake("ovwhisper", "Go ahead."))
    assert run_worker(t.cfg, transcriber=t, drain=True) == 3
    r = rows(tmp_path)
    assert all(r[i]["status"] == "done" and r[i]["transcript"] == "Go ahead." for i in ids)
    assert all(r[i]["stt_ms"] is not None and r[i]["transcribed_at"] for i in ids)
    conn = db.connect(tmp_path / "scanner.db")
    from scanner_transcriber import statestore
    w = statestore.states(conn)["worker"]
    kinds = [e["kind"] for e in statestore.recent_events(conn)]
    conn.close()
    assert w["state"] == "stopped" and w["detail"]["processed"] == 3
    assert kinds == ["stopped", "started"]


def test_run_worker_retries_then_fails(tmp_path):
    [tx] = queue_clips(tmp_path, 1)
    t = make(tmp_path, Fake("ovwhisper", boom=True), max_attempts=2)
    run_worker(t.cfg, transcriber=t, drain=True)
    r = rows(tmp_path)[tx]
    assert r["status"] == "failed" and r["attempts"] == 2 and "decoder exploded" in r["error"]


def test_run_worker_stops_and_releases_claims(tmp_path):
    queue_clips(tmp_path, 1)
    stop = threading.Event()
    t = make(tmp_path, Fake("ovwhisper", "clear"))
    th = threading.Thread(target=run_worker, args=(t.cfg,),
                          kwargs={"transcriber": t, "stop": stop})
    th.start()
    for _ in range(100):
        if all(r["status"] == "done" for r in rows(tmp_path).values()):
            break
        threading.Event().wait(0.05)
    stop.set()
    th.join(5)
    assert not th.is_alive()
    assert [r["status"] for r in rows(tmp_path).values()] == ["done"]


def test_real_one_word_reply_survives_speech_check(tmp_path):
    # Parakeet hearing even one word means there was speech: don't blank
    r = make(tmp_path, Fake("ovwhisper", "Thank you."), Fake("sherpa", "okay")).transcribe(X)
    assert not r.no_speech and r.text == "Thank you."


def test_worker_survives_a_database_error_in_its_loop(tmp_path, monkeypatch):
    import sqlite3
    from scanner_transcriber import jobs, worker
    ids = queue_clips(tmp_path, 2)
    t = make(tmp_path, Fake("ovwhisper", "Go ahead."))
    monkeypatch.setattr(worker, "LOOP_ERROR_PAUSE_S", 0.01)
    real, calls = jobs.claim, []

    def locked_once(conn, worker_id):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(conn, worker_id)

    monkeypatch.setattr(jobs, "claim", locked_once)
    assert run_worker(t.cfg, transcriber=t, drain=True) == 2
    assert all(rows(tmp_path)[i]["status"] == "done" for i in ids)



class Recorder(Fake):
    def __init__(self):
        super().__init__("ovwhisper", "Direct.")
        self.lengths = []

    def transcribe(self, pcm, *, use_prompt=True):
        self.lengths.append(len(pcm))
        return super().transcribe(pcm, use_prompt=use_prompt)


def test_short_clips_get_leading_silence_before_transcription(tmp_path):
    rec = Recorder()
    t = make(tmp_path, rec)
    short = np.ones(16000, np.float32)                  # 1.0 s
    long_ = np.ones(48000, np.float32)                  # 3.0 s
    t.transcribe(short)
    t.transcribe(long_)
    assert rec.lengths == [16000 + 8000, 48000]         # 0.5 s in front of the short one only


def test_leading_silence_can_be_turned_off(tmp_path):
    rec = Recorder()
    t = make(tmp_path, rec, lead_silence_ms=0)
    t.transcribe(np.ones(16000, np.float32))
    assert rec.lengths == [16000]
