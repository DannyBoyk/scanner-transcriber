"""NVIDIA Parakeet (and other ONNX exports) via onnx-asr + onnxruntime.

extra `provider=openvino` (with `device=GPU|CPU`) needs the .venv-ov environment
(onnxruntime-openvino).
"""
from __future__ import annotations

import numpy as np

from .base import SttConfig, SttResult


class OnnxAsrBackend:
    name = "onnxasr"

    def load(self, cfg: SttConfig) -> None:
        import onnx_asr
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = cfg.threads
        opts.inter_op_num_threads = 1
        if cfg.extra.get("provider") == "openvino":
            providers = [("OpenVINOExecutionProvider",
                          {"device_type": cfg.extra.get("device", "GPU")}), "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        path = cfg.extra.get("path")  # plain directory (HF cache symlinks trip ORT's check)
        self.model = onnx_asr.load_model(
            cfg.model, path, quantization=cfg.extra.get("quantization"), sess_options=opts,
            providers=providers,
        )

    def transcribe(self, pcm16k: np.ndarray) -> SttResult:
        text = self.model.recognize(pcm16k, sample_rate=16000)
        text = (text or "").strip()
        return SttResult(text=text, confidence=None, no_speech=not text)
