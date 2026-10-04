from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import numpy as np


@dataclass
class SttConfig:
    model: str
    threads: int = 3
    prompt: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class SttResult:
    text: str
    confidence: float | None = None  # normalized 0-1 where the backend offers one
    segments: list[dict] | None = None
    no_speech: bool = False


class SttBackend(Protocol):
    name: str

    def load(self, cfg: SttConfig) -> None: ...

    def transcribe(self, pcm16k: np.ndarray) -> SttResult: ...  # float32 mono, 16 kHz


def get_backend(name: str) -> SttBackend:
    if name == "fasterwhisper":
        from .fasterwhisper import FasterWhisperBackend
        return FasterWhisperBackend()
    if name == "onnxasr":
        from .onnxasr import OnnxAsrBackend
        return OnnxAsrBackend()
    if name == "moonshine":
        from .moonshine import MoonshineBackend
        return MoonshineBackend()
    if name == "sherpa":
        from .sherpa import SherpaBackend
        return SherpaBackend()
    if name == "ovwhisper":
        from .ovwhisper import OvWhisperBackend
        return OvWhisperBackend()
    raise ValueError(f"unknown STT backend {name!r}")
