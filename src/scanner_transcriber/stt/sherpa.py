"""sherpa-onnx offline recognizers (e.g. Parakeet TDT-CTC 110M, NeMo CTC export).

`model` is a directory under models/ containing model*.onnx + tokens.txt.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .base import SttConfig, SttResult


class SherpaBackend:
    name = "sherpa"

    def load(self, cfg: SttConfig) -> None:
        import sherpa_onnx

        d = Path(cfg.extra.get("models_dir", "models")) / cfg.model
        self.model_name = cfg.model
        onnx = sorted(d.glob("model*.onnx"))
        if not onnx:
            raise FileNotFoundError(f"no model*.onnx in {d}")
        self.rec = sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=str(onnx[0]), tokens=str(d / "tokens.txt"), num_threads=cfg.threads,
        )

    def transcribe(self, pcm16k: np.ndarray) -> SttResult:
        s = self.rec.create_stream()
        s.accept_waveform(16000, pcm16k.astype(np.float32))
        self.rec.decode_stream(s)
        text = s.result.text.strip()
        return SttResult(text=text, confidence=None, no_speech=not text)
