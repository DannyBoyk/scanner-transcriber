"""Prompt/substitution versions: import once, save/activate/revert, and worker hot reload."""
from dataclasses import replace

from scanner_transcriber import db, promptstore
from scanner_transcriber.config import Config, StorageConfig, TranscribeConfig
from scanner_transcriber.stt.base import SttResult
from scanner_transcriber.worker import Transcriber, run_worker

from test_worker import queue_clips, rows


def cfg(tmp_path, **stt):
    return replace(Config(), storage=StorageConfig(data_dir=tmp_path), stt=TranscribeConfig(**stt))


def test_import_from_config_once(conn, tmp_path):
    c = cfg(tmp_path, prompt="Northfield, Ashford.", substitutions={"Charley": "Charlie"})
    assert promptstore.ensure_seeded(conn, c) == []
    vid, p = promptstore.active(conn, "prompt")
    assert p == "Northfield, Ashford." and promptstore.history(conn, "prompt")[0]["note"] == \
        "imported from config.toml"
    assert promptstore.active(conn, "substitutions")[1] == {"Charley": "Charlie"}
    assert promptstore.ensure_seeded(conn, c) == []                         # not re-imported
    assert len(promptstore.history(conn, "prompt")) == 1


def test_default_when_config_empty_and_warning_when_config_differs(conn, tmp_path):
    promptstore.ensure_seeded(conn, cfg(tmp_path))
    assert promptstore.active(conn, "prompt")[1] == promptstore.DEFAULT_PROMPT
    warns = promptstore.ensure_seeded(conn, cfg(tmp_path, prompt="something else"))
    assert warns and "ignored" in warns[0] and "Admin page" in warns[0]


def test_save_activate_revert(conn, tmp_path):
    promptstore.ensure_seeded(conn, cfg(tmp_path, prompt="v1 text"))
    v1 = promptstore.active(conn, "prompt")[0]
    v2 = promptstore.save(conn, "prompt", "v2 text", note="added Direct")
    assert promptstore.active(conn, "prompt") == (v2, "v2 text")
    assert promptstore.activate(conn, v1) == "prompt"
    assert promptstore.active(conn, "prompt") == (v1, "v1 text")
    h = promptstore.history(conn, "prompt")
    assert [x["id"] for x in h] == [v2, v1] and [x["active"] for x in h] == [False, True]


class PromptEcho:
    """Fake Whisper backend: its output is the prompt it was given (to observe hot reload)."""
    name = "ovwhisper"

    def __init__(self):
        self.prompt = None

    def load(self, c):
        self.prompt = c.prompt

    def transcribe(self, pcm, *, use_prompt=True):
        return SttResult(text=f"heard with [{self.prompt}] Charley")


class Silent:
    name = "sherpa"

    def load(self, c):
        pass

    def transcribe(self, pcm):
        return SttResult(text="")


def test_worker_hot_reloads_prompt_and_subs_without_restart(tmp_path):
    c = cfg(tmp_path, prompt="first prompt")
    ids = queue_clips(tmp_path, 1)
    t = Transcriber(c, primary=PromptEcho(), fallback=Silent())
    t.load()
    run_worker(c, transcriber=t, drain=True)
    r = rows(tmp_path)[ids[0]]
    assert "first prompt" in r["transcript"] and r["prompt_version"] is not None
    first_version = r["prompt_version"]

    conn = db.connect(tmp_path / "scanner.db")
    promptstore.save(conn, "prompt", "second prompt", note="edit")
    promptstore.save(conn, "substitutions", {"Charley": "Charlie"})
    conn.close()
    [tx2] = queue_clips(tmp_path, 1)
    run_worker(c, transcriber=t, drain=True)                     # same Transcriber: no reload
    r2 = rows(tmp_path)[tx2]
    assert "second prompt" in r2["transcript"] and r2["transcript"].endswith("Charlie")
    assert r2["prompt_version"] != first_version and r2["subs_version"] is not None
