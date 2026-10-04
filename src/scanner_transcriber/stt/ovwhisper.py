"""Whisper via OpenVINO GenAI, e.g. on the NUC's UHD iGPU.

`model` is a Hugging Face repo of a pre-converted model (e.g. OpenVINO/whisper-small.en-int8-ov).
extra: `device` = GPU|CPU, `cache_dir` = where compiled GPU kernels are cached (fast restarts).
"""
from __future__ import annotations

import logging

import numpy as np

from .base import SttConfig, SttResult

log = logging.getLogger(__name__)


def _model_path(repo: str) -> str:
    """Use the local copy if present (no network needed at startup); download otherwise."""
    from huggingface_hub import snapshot_download

    try:
        return snapshot_download(repo, local_files_only=True)
    except Exception:
        log.info("downloading %s", repo)
        return snapshot_download(repo)


class OvWhisperBackend:
    name = "ovwhisper"

    def load(self, cfg: SttConfig) -> None:
        import openvino_genai

        self.model_name = cfg.model
        path = _model_path(cfg.model)
        device = cfg.extra.get("device", "GPU")
        props = {} if device == "GPU" else {"INFERENCE_NUM_THREADS": cfg.threads}
        if cfg.extra.get("cache_dir"):
            props["CACHE_DIR"] = str(cfg.extra["cache_dir"])
        self.pipe = openvino_genai.WhisperPipeline(path, device, **props)
        self.gen = self.pipe.get_generation_config()
        self.gen.max_new_tokens = 128
        if ".en-" not in cfg.model:  # multilingual checkpoints: pin English
            self.gen.language = "<|en|>"
            self.gen.task = "transcribe"
        self.prompt = cfg.prompt or None

    def transcribe(self, pcm16k: np.ndarray, *, use_prompt: bool = True) -> SttResult:
        self.gen.initial_prompt = self.prompt if use_prompt else None
        result = self.pipe.generate(pcm16k.astype(np.float32).tolist(), self.gen)
        text = str(result.texts[0] if hasattr(result, "texts") else result).strip()
        return SttResult(text=text, confidence=None, no_speech=not text)
