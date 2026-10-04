"""Useful Sensors Moonshine via its ONNX package."""
from __future__ import annotations

from unittest import mock

import numpy as np

from .base import SttConfig, SttResult


class MoonshineBackend:
    name = "moonshine"

    def load(self, cfg: SttConfig) -> None:
        import onnxruntime as ort
        from moonshine_onnx import MoonshineOnnxModel, load_tokenizer

        # The package creates its sessions without options; inject a thread cap.
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = cfg.threads
        opts.inter_op_num_threads = 1
        real = ort.InferenceSession

        def session(path, *a, **kw):
            kw.setdefault("sess_options", opts)
            kw.setdefault("providers", ["CPUExecutionProvider"])
            return real(path, *a, **kw)

        with mock.patch("onnxruntime.InferenceSession", session):
            self.model = MoonshineOnnxModel(model_name=f"moonshine/{cfg.model}")
        self.tokenizer = load_tokenizer()

    def transcribe(self, pcm16k: np.ndarray) -> SttResult:
        tokens = self.model.generate(pcm16k[np.newaxis, :].astype(np.float32))
        text = self.tokenizer.decode_batch(tokens)[0].strip()
        return SttResult(text=text, confidence=None, no_speech=not text)
