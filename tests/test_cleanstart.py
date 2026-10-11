"""Clean start: deletes transmissions, audio, events, batches and logs; keeps settings."""
import asyncio
import os
import time
from dataclasses import replace

from test_feed import add

from scanner_transcriber import cleanstart, commands, promptstore, statestore
from scanner_transcriber.app import handle_commands
from scanner_transcriber.config import Config, StorageConfig


def populate(conn, d):
    ids = [add(conn, d, i * 60) for i in range(5)]
    conn.execute("UPDATE channels SET alias = 'Sheriff dispatch'")
    promptstore.save(conn, "prompt", "Ashford, Brookfield.")
    conn.execute("INSERT INTO prompt_evals (created_at, status, clips, wer, cer, detail) VALUES ('2026-09-30T12:00:00+00:00', 'done', 2, 0.1, 0.05, '{}')")
    conn.execute("INSERT INTO retranscribe_batches (created_at, total) VALUES ('2026-09-30T12:00:00+00:00', 2)")
    conn.execute("UPDATE transmissions SET batch_id = 1")
    statestore.event(conn, "capture", "info", "connected", "Scanner connected")
    (d / "clips" / "orphan.wav").write_bytes(b"x" * 100)          # no row points to it
    (d / "spill").mkdir()
    (d / "spill" / "2026-09-30_120000_TG1101.wav").write_bytes(b"y" * 50)
    (d / "sessions" / "s1" / "clips").mkdir(parents=True)
    (d / "sessions" / "s1" / "clips" / "0001.wav").write_bytes(b"ref")
    (d / "sessions" / "s1" / "refs.jsonl").write_text('{"id": "0001", "text": "x"}\n')
    (d / "logs").mkdir()
    (d / "logs" / "capture.log").write_text("old line\n")
    (d / "logs" / "capture.log.1").write_text("older\n")
    return ids


def cfg_for(d):
    return replace(Config(), storage=StorageConfig(data_dir=d))


def test_preview_counts(env):
    conn, _, d = env
    populate(conn, d)
    p = cleanstart.preview(conn, cfg_for(d))
    assert p.transmissions == 5 and p.audio_files == 7 and p.batches == 1 and p.events >= 1
    assert p.log_files == 2 and "would remove 5 transmissions" in p.text(dry_run=True)


def test_clean_start_removes_data_and_keeps_settings(env):
    conn, _, d = env
    populate(conn, d)
    s = cleanstart.run(conn, cfg_for(d))
    assert s.transmissions == 5 and s.audio_files == 7 and s.batches == 1 and not s.errors
    assert conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM transmissions_fts WHERE transmissions_fts MATCH 'medic'"
                        ).fetchone()[0] == 0
    assert not any((d / "clips").rglob("*.wav")) and not any((d / "spill").rglob("*"))
    # kept
    assert conn.execute("SELECT alias FROM channels").fetchone()[0] == "Sheriff dispatch"
    assert promptstore.active(conn, "prompt")[1] == "Ashford, Brookfield."
    assert conn.execute("SELECT COUNT(*) FROM prompt_evals").fetchone()[0] == 1
    assert (d / "sessions" / "s1" / "clips" / "0001.wav").exists()
    # event log starts fresh with the clean start itself; logs truncated, rotated ones gone
    kinds = [e["kind"] for e in statestore.recent_events(conn)]
    assert kinds == ["clean_start"]
    assert (d / "logs" / "capture.log").read_text() == ""
    assert not (d / "logs" / "capture.log.1").exists()


def test_audio_saved_after_the_start_is_kept(env):
    conn, _, d = env
    populate(conn, d)
    fresh = d / "clips" / "2026" / "10" / "01" / "new.wav"
    fresh.parent.mkdir(parents=True)
    fresh.write_bytes(b"z")
    future = time.time() + 60                                       # "written while it ran"
    os.utime(fresh, (future, future))
    cleanstart.run(conn, cfg_for(d))
    assert fresh.exists()


def test_capture_runs_the_admin_command(env):
    conn, _, d = env
    populate(conn, d)
    commands.post(conn, "capture", "clean-start")
    conn.commit()
    asyncio.run(handle_commands(cfg_for(d), object(), None))
    assert conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 0
    result = conn.execute("SELECT result, done_at FROM commands").fetchone()
    assert result["done_at"] and "removed 5 transmissions" in result["result"]


def test_admin_section_needs_login_and_typed_confirmation(env):
    from conftest import login
    conn, client, d = env
    populate(conn, d)
    hx = {"HX-Request": "true"}
    assert client.get("/admin/fragments/clean").status_code in (401, 403)
    login(client, conn)
    html = client.get("/admin").text
    assert 'id="clean"' in html and "Check what it would delete" in html
    frag = client.get("/admin/fragments/clean").text
    assert "<b>5</b> transmissions" in frag and 'name="confirm"' in frag
    r = client.post("/admin/clean-start", data={"confirm": "delete"}, headers=hx)
    assert "type DELETE" in r.text
    assert conn.execute("SELECT COUNT(*) FROM commands WHERE action = 'clean-start'").fetchone()[0] == 0
    r = client.post("/admin/clean-start", data={"confirm": "DELETE"}, headers=hx)
    assert "queued" in r.text
    assert conn.execute("SELECT COUNT(*) FROM commands WHERE action = 'clean-start'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 5   # capture does it


def test_cli_clean_start(env, monkeypatch, capsys):
    from scanner_transcriber.__main__ import main
    conn, _, d = env
    populate(conn, d)
    cfg_path = d / "cfg.toml"
    cfg_path.write_text(f'[storage]\ndata_dir = "{d}"\n')
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert main(["--config", str(cfg_path), "clean-start"]) == 1
    assert conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 5
    main(["--config", str(cfg_path), "clean-start", "--yes"])
    assert conn.execute("SELECT COUNT(*) FROM transmissions").fetchone()[0] == 0
    assert "removed 5 transmissions" in capsys.readouterr().out
