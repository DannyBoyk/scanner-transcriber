"""Transcription worker: claims queued clips, transcribes, applies guards, stores results.

One process. Primary model: whisper-small.en on the iGPU with the vocabulary prompt.
Parakeet 110M (CPU) is loaded too: it takes over entirely if the GPU model can't load
("degraded" mode), and otherwise only acts as a speech check on known hallucinations.
"""
from __future__ import annotations

import inspect
import logging
import os
import signal
import socket
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import commands, db, guards, jobs, prompteval, promptstore, statestore
from .audio import STT_RATE, read_wav, resample
from .config import Config
from .stt.base import SttBackend, SttConfig, get_backend

log = logging.getLogger(__name__)

HEARTBEAT_S = 10  # worker heartbeat; health "worker down" threshold is 30 s (3 missed beats)
LOOP_ERROR_PAUSE_S = 2  # after an unexpected error in the loop (not a clip's own failure)


@dataclass
class Transcript:
    text: str
    raw: str
    alt: str | None = None
    flags: list[str] = field(default_factory=list)
    no_speech: bool = False
    backend: str = ""
    model: str = ""


class Transcriber:
    def __init__(self, cfg: Config, *, primary: SttBackend | None = None,
                 fallback: SttBackend | None = None) -> None:
        self.cfg = cfg
        self.primary = primary
        self.fallback = fallback
        self.degraded = False
        self.degraded_reason: str | None = None
        # live-editable (Admin page): replaced from the database by `apply_versions`
        self.prompt: str = cfg.stt.prompt
        self.subs: dict[str, str] = dict(cfg.stt.substitutions)

    def set_prompt(self, prompt: str) -> None:
        """Hot-swap the Whisper prompt: no model reload (it's just generation config)."""
        self.prompt = prompt
        if self.primary is not None and hasattr(self.primary, "prompt"):
            self.primary.prompt = prompt or None

    @property
    def primary_model_name(self) -> str:
        return self.cfg.stt.degraded_model if self.degraded else self.cfg.stt.model

    @property
    def mode(self) -> str:
        return "degraded" if self.degraded else "gpu" if self.cfg.stt.device == "GPU" else "cpu"

    def load(self) -> None:
        s = self.cfg.stt
        self.fallback = self.fallback or get_backend("sherpa")
        self.fallback.load(SttConfig(model=s.degraded_model, threads=s.degraded_threads,
                                     extra={"models_dir": s.models_dir}))
        try:
            self.primary = self.primary or get_backend("ovwhisper")
            self.cfg.storage.ov_cache_dir.mkdir(parents=True, exist_ok=True)
            self.primary.load(SttConfig(
                model=s.model, threads=1, prompt=self.prompt or None,
                extra={"device": s.device, "cache_dir": self.cfg.storage.ov_cache_dir}))
        except Exception as e:  # noqa: BLE001 — any GPU/driver failure → keep transcribing
            self.degraded, self.degraded_reason = True, f"{type(e).__name__}: {e}"
            log.warning("PRIMARY MODEL UNAVAILABLE (%s) — degraded mode: %s only, lower accuracy",
                        self.degraded_reason, s.degraded_model)

    def _lead_silence(self, x: np.ndarray) -> np.ndarray:
        """16 kHz audio of a short clip → the same with lead_silence_ms of silence in front."""
        s = self.cfg.stt
        if s.lead_silence_ms <= 0 or len(x) > s.lead_silence_max_clip_ms * STT_RATE // 1000:
            return x
        return np.concatenate([np.zeros(s.lead_silence_ms * STT_RATE // 1000, x.dtype), x])

    def _primary_text(self, x: np.ndarray, use_prompt: bool = True) -> str:
        if "use_prompt" in inspect.signature(self.primary.transcribe).parameters:
            return self.primary.transcribe(x, use_prompt=use_prompt).text
        return self.primary.transcribe(x).text  # backend without prompt control

    def transcribe(self, x: np.ndarray) -> Transcript:
        s = self.cfg.stt
        x = self._lead_silence(x)
        if self.degraded:
            text = self.fallback.transcribe(x).text
            t = Transcript(text=text, raw=text, flags=["degraded"],
                           backend=self.fallback.name, model=s.degraded_model)
        else:
            text = self._primary_text(x)
            t = Transcript(text=text, raw=text, backend=self.primary.name, model=s.model)
            if guards.prompt_leak(text, self.prompt):
                t.flags.append("prompt_leak")
                retry = self._primary_text(x, use_prompt=False)
                if not (guards.is_blank(retry) or guards.is_hallucination(retry)
                        or guards.prompt_leak(retry, self.prompt)):
                    t.text = retry

        t.text, repeated = guards.collapse_repeats(t.text)
        if repeated:
            t.flags.append("repeat")

        if guards.is_blank(t.text):
            t.text, t.no_speech = "", True
        elif not self.degraded and guards.is_hallucination(t.text):
            # Whisper filler on faint/no audio? Ask Parakeet whether there was speech at all.
            t.flags.append("hallucination")
            t.alt = self.fallback.transcribe(x).text.strip()
            if guards.is_blank(t.alt):
                t.text, t.no_speech = "", True

        t.text = guards.substitute(t.text, self.subs)
        return t


def run_worker(cfg: Config, *, stop: threading.Event | None = None,
               transcriber: Transcriber | None = None, drain: bool = False) -> int:
    """Process clips until `stop` is set (or, with drain=True, until the queue is empty).
    Returns the number of clips processed."""
    stop = stop or threading.Event()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    conn = db.connect(cfg.storage.db_path)
    n = jobs.recover_dead(conn, socket.gethostname())
    n += jobs.recover_stale(conn, cfg.queue.claim_timeout_s)
    if n:
        log.info("re-queued %d clip(s) left by a previous worker", n)
    if prompteval.recover_interrupted(conn):
        log.info("marked a prompt test interrupted by the restart as failed")
    t = transcriber or Transcriber(cfg)
    for w in promptstore.ensure_seeded(conn, cfg):
        log.warning(w)
    versions: dict[str, int | None] = {}

    def apply_versions() -> None:
        """Pick up prompt / substitution edits from the Admin page (checked before each clip)."""
        ids = promptstore.active_ids(conn)
        if ids == versions:
            return
        if ids.get("prompt") != versions.get("prompt"):
            _, prompt = promptstore.active(conn, "prompt")
            t.set_prompt(prompt)
            if versions:
                log.info("prompt version %s now active", ids.get("prompt"))
        if ids.get("substitutions") != versions.get("substitutions"):
            _, t.subs = promptstore.active(conn, "substitutions")
            if versions:
                log.info("substitutions version %s now active", ids.get("substitutions"))
        versions.clear()
        versions.update(ids)

    apply_versions()
    if transcriber is None:
        t0 = time.monotonic()
        t.load()
        log.info("worker ready in %.1fs (mode=%s)", time.monotonic() - t0, t.mode)
    done = 0
    last_recover = last_hb = 0.0
    evaluation: prompteval.EvalRun | None = None

    def heartbeat(state: str | None = None, current: int | None = None) -> None:
        statestore.publish(conn, "worker", state or t.mode, {
            "model": t.primary_model_name, "mode": t.mode,
            "degraded_reason": t.degraded_reason, "processed": done, "current": current})

    statestore.event(conn, "worker", "info", "started",
                     f"Transcription worker started ({t.mode}: {t.primary_model_name})")
    if t.degraded:
        statestore.event(conn, "worker", "warn", "degraded",
                         f"Degraded mode (CPU fallback, lower accuracy): {t.degraded_reason}")
    try:
        while not stop.is_set():
            try:
                now = time.monotonic()
                if now - last_hb >= HEARTBEAT_S:
                    heartbeat()
                    last_hb = now
                if now - last_recover >= 60:
                    jobs.recover_stale(conn, cfg.queue.claim_timeout_s)
                    last_recover = now
                apply_versions()
                row = jobs.claim(conn, worker_id)
                if row is None:
                    # idle: work on a prompt test (one clip at a time, so live clips cut in)
                    if evaluation is None:
                        cmds = commands.take(conn, "worker", {"prompt-test"})
                        prompteval.fail_expired(conn)
                        for cmd in cmds:
                            evaluation = prompteval.start(conn, cmd, cfg.storage.data_dir)
                            if evaluation:
                                break
                    if evaluation is not None:
                        try:
                            evaluation.step(t, promptstore.active(conn, "prompt")[1])
                            evaluation.progress(conn)
                            if evaluation.finished:
                                wer, cer = evaluation.finish(conn)
                                commands.done(conn, evaluation.command, f"WER {wer:.3f}, "
                                              f"CER {cer:.3f} on {evaluation.i} clips")
                                log.info("prompt test %s done: WER %.3f", evaluation.eval_id, wer)
                                evaluation = None
                        except Exception as e:  # noqa: BLE001
                            log.exception("prompt test failed")
                            conn.execute("UPDATE prompt_evals SET status='failed' WHERE id=?",
                                         (evaluation.eval_id,))
                            commands.done(conn, evaluation.command, f"failed: {e}", "warn")
                            evaluation = None
                        continue
                    if drain:
                        break
                    stop.wait(cfg.stt.poll_interval_s)
                    continue
                try:
                    pcm, rate = read_wav(cfg.storage.data_dir / row["audio_path"])
                    start = time.perf_counter()
                    result = t.transcribe(resample(pcm, rate))
                    stt_ms = int((time.perf_counter() - start) * 1000)
                    jobs.finish(conn, row["id"], transcript=result.text, raw_transcript=result.raw,
                                alt_transcript=result.alt, flags=result.flags,
                                no_speech=result.no_speech, stt_backend=result.backend,
                                stt_model=result.model, stt_ms=stt_ms,
                                prompt_version=None if t.degraded else versions.get("prompt"),
                                subs_version=versions.get("substitutions"))
                    done += 1
                    log.info("tx %d %.1fs in %.1fs%s: %s", row["id"], row["duration_ms"] / 1000,
                             stt_ms / 1000, f" {result.flags}" if result.flags else "",
                             result.text or "<no speech>")
                except Exception as e:  # noqa: BLE001 — one bad clip must not stop the worker
                    status = jobs.fail(conn, row["id"], f"{type(e).__name__}: {e}",
                                       max_attempts=cfg.stt.max_attempts)
                    log.exception("tx %d failed (%s)", row["id"], status)
            except Exception:  # noqa: BLE001 — e.g. database locked: log, pause, go on
                log.exception("worker loop error; retrying in %ss", LOOP_ERROR_PAUSE_S)
                stop.wait(LOOP_ERROR_PAUSE_S)
    finally:
        try:
            jobs.release_mine(conn, worker_id)
            heartbeat(state="stopped")
            statestore.event(conn, "worker", "info", "stopped",
                             f"Transcription worker stopped ({done} clips this run)")
        except Exception:  # noqa: BLE001 — stale claims are recovered by the next worker
            log.exception("could not record the worker stopping")
        conn.close()
    return done


def worker_process_main(cfg: Config, log_level: int = logging.INFO) -> None:
    """Entry point for the spawned worker process: SIGTERM/SIGINT finish the current clip."""
    from .logsetup import setup
    setup("worker", cfg.storage.data_dir, log_level)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    run_worker(cfg, stop=stop)
