from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ScannerConfig:
    id: str = "sds200-1"
    host: str = "192.168.1.11"
    control_port: int = 50536
    rtsp_port: int = 554
    psi_interval_ms: int = 250
    # Audio lockout recovery (docs/sds200-rtsp-lockout.md): if the scanner refuses audio
    # connections while its control channel answers, reboot it with the (undocumented)
    # MSM,0 command. Never within `auto_reboot_grace_s` of capture starting, at most once per
    # `auto_reboot_min_interval_s`, and after `auto_reboot_max_tries` reboots without audio
    # coming back, stop and ask for a power cycle.
    auto_reboot: bool = True
    auto_reboot_after_s: float = 180
    auto_reboot_grace_s: float = 150
    auto_reboot_min_interval_s: float = 900
    auto_reboot_max_tries: int = 3


@dataclass(frozen=True)
class SegmenterConfig:
    debounce_ms: int = 400        # Mute shorter than this inside one PTT is ignored
    preroll_ms: int = 300         # audio kept from before the Unmute status
    pad_ms: int = 100             # silence kept around trimmed speech
    min_clip_ms: int = 400        # minimum non-silent span to keep a clip
    max_clip_s: int = 60          # longer transmissions are split ("continued")
    audio_latency_ms: int = 0     # status is applied this much later on the audio timeline
    status_stale_ms: int = 2000   # no status for this long → segment on audio alone
    split_on_unit_change: bool = True  # scanner can stay unmuted across two units' PTTs
    unit_gap_ms: int = 200        # silence that marks the handoff when splitting on a unit change


@dataclass(frozen=True)
class TranscribeConfig:
    """[stt]: the transcription worker (whisper-small.en on the iGPU, Parakeet on the CPU)."""
    model: str = "OpenVINO/whisper-small.en-int8-ov"
    device: str = "GPU"
    degraded_model: str = "sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000-int8"  # CPU
    degraded_threads: int = 2
    models_dir: Path = Path("models")
    prompt: str = ""
    substitutions: dict[str, str] = field(default_factory=dict)
    max_attempts: int = 3
    poll_interval_s: float = 0.5
    # Short clips get this much silence in front before transcription (not in the stored audio):
    # Whisper fumbles a first word that starts at the very beginning of its input. Measured
    # 2026-10-01: short-clip WER 0.361 → 0.299 (and 0.459 → 0.426 on the held-out set); longer
    # clips got worse with it, so only clips up to lead_silence_max_clip_ms. 0 turns it off.
    lead_silence_ms: int = 500
    lead_silence_max_clip_ms: int = 2000


@dataclass(frozen=True)
class QueueConfig:
    claim_timeout_s: float = 120


@dataclass(frozen=True)
class RetentionConfig:
    """[retention]: what's deleted when (0 = keep forever). See docs/m5-plan.md."""
    audio_days: int = 90          # clip WAVs (row + transcript kept)
    raw_days: int = 90            # raw scanner status XML / status changes on each row
    transcript_days: int = 365    # whole rows: transcript, metadata, search entries
    event_days: int = 30          # status-page event log
    command_days: int = 30        # finished Admin commands
    prompt_tests_keep: int = 100  # newest Admin prompt-test results
    run_every_minutes: int = 60


@dataclass(frozen=True)
class WebConfig:
    """[web]: the web UI process (`web` command)."""
    bind: str = "127.0.0.1"       # M3: the LAN address; M5: localhost behind tailscale serve
    port: int = 8080
    page_size: int = 50
    live_max_rows: int = 300      # live feed trims its oldest rows beyond this (browser memory)
    timezone: str = ""            # IANA name, e.g. "America/New_York"; empty = system local


@dataclass(frozen=True)
class StatusConfig:
    """[status]: when the header dot turns amber/red (see docs/m3-plan.md)."""
    capture_stale_s: float = 15       # capture heartbeat older than this = not running
    worker_stale_s: float = 30
    reconnect_error_s: float = 60     # reconnecting longer than this is red (shorter: amber)
    backlog_warn_s: float = 120       # oldest queued clip waiting longer than this
    backlog_error_s: float = 600
    disk_warn_pct: float = 10
    disk_warn_gb: float = 5
    disk_error_gb: float = 2
    failed_window_h: float = 1        # any failed clip in this window → amber


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path = Path("data")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scanner.db"

    @property
    def ov_cache_dir(self) -> Path:
        return self.data_dir / "ov-cache"  # compiled OpenVINO GPU models (fast restarts)


@dataclass(frozen=True)
class Config:
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    segmenter: SegmenterConfig = field(default_factory=SegmenterConfig)
    stt: TranscribeConfig = field(default_factory=TranscribeConfig)
    queue: QueueConfig = field(default_factory=QueueConfig)
    status: StatusConfig = field(default_factory=StatusConfig)
    web: WebConfig = field(default_factory=WebConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)


def load_config(path: str | Path | None) -> Config:
    if path is None or not Path(path).exists():
        return Config()
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    storage = dict(raw.get("storage", {}))
    if "data_dir" in storage:
        storage["data_dir"] = Path(storage["data_dir"])
    stt = dict(raw.get("stt", {}))
    if "models_dir" in stt:
        stt["models_dir"] = Path(stt["models_dir"])
    return Config(
        scanner=ScannerConfig(**raw.get("scanner", {})),
        segmenter=SegmenterConfig(**raw.get("segmenter", {})),
        stt=TranscribeConfig(**stt),
        queue=QueueConfig(**raw.get("queue", {})),
        status=StatusConfig(**raw.get("status", {})),
        web=WebConfig(**raw.get("web", {})),
        retention=RetentionConfig(**raw.get("retention", {})),
        storage=StorageConfig(**storage),
    )
