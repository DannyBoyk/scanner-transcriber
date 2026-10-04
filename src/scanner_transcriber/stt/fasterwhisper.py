"""Whisper via CTranslate2 (faster-whisper). Runs on non-AVX CPUs via runtime dispatch."""
from __future__ import annotations

import math

import numpy as np

from .base import SttConfig, SttResult


class FasterWhisperBackend:
    name = "fasterwhisper"

    def load(self, cfg: SttConfig) -> None:
        from faster_whisper import WhisperModel

        self.cfg = cfg
        self.model = WhisperModel(
            cfg.model, device="cpu", compute_type=cfg.extra.get("compute_type", "int8"),
            cpu_threads=cfg.threads, download_root=cfg.extra.get("download_root"),
        )

    def transcribe(self, pcm16k: np.ndarray) -> SttResult:
        segments, _info = self.model.transcribe(
            pcm16k,
            language="en",
            beam_size=self.cfg.extra.get("beam_size", 1),
            initial_prompt=self.cfg.prompt,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        segs = list(segments)
        text = " ".join(s.text.strip() for s in segs).strip()
        if not segs:
            return SttResult(text="", confidence=None, segments=[], no_speech=True)
        conf = float(np.mean([math.exp(s.avg_logprob) for s in segs]))
        no_speech = all(s.no_speech_prob > 0.6 and s.avg_logprob < -1.0 for s in segs)
        return SttResult(
            text=text, confidence=conf, no_speech=no_speech,
            segments=[{"text": s.text, "avg_logprob": s.avg_logprob,
                       "no_speech_prob": s.no_speech_prob} for s in segs],
        )
