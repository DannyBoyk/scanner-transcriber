from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scanner_transcriber")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="UDP-only: model, firmware, status samples (safe)")
    p.add_argument("--psi-seconds", type=float, default=10.0)

    p = sub.add_parser("record", help="Record audio + status from the scanner (owns the RTSP session)")
    p.add_argument("--duration", type=float, default=None, help="seconds; default until Ctrl-C")

    p = sub.add_parser("run", help="Capture → segment → store queued clips")
    p.add_argument("--replay", type=Path, default=None,
                   help="replay a session recorded by `record` instead of the live scanner")
    p.add_argument("--speed", choices=["max", "realtime"], default="max")
    p.add_argument("--no-worker", action="store_true", help="capture only; don't transcribe")
    p.add_argument("--drain", action="store_true",
                   help="with --replay: keep transcribing after the replay until the queue is empty")

    p = sub.add_parser("worker", help="Run only the transcription worker (e.g. alongside run --no-worker)")
    p.add_argument("--drain", action="store_true", help="exit when the queue is empty")

    p = sub.add_parser("web", help="Serve the web UI (separate process from `run`)")
    p.add_argument("--bind", default=None, help="override [web] bind address")
    p.add_argument("--port", type=int, default=None)

    p = sub.add_parser("retention", help="Apply retention now (see [retention] in config)")
    p.add_argument("--dry-run", action="store_true", help="only report what would be deleted")
    p.add_argument("--vacuum", action="store_true",
                   help="also compact the database file (never automatic; writers wait meanwhile)")

    p = sub.add_parser("clean-start", help="Delete all transmissions, audio, events and logs "
                       "(keeps talkgroup settings, prompts, labelled clips); asks first")
    p.add_argument("--yes", action="store_true", help="don't ask (scripts)")

    p = sub.add_parser("set-password", help="Set the admin password (Admin page + talkgroup edits)")
    p.add_argument("--clear", action="store_true", help="remove it (turns admin off)")

    p = sub.add_parser("evaluate", help="Score DB transcripts of a replayed session against its hand labels")
    p.add_argument("session", type=Path)
    p.add_argument("--show", type=int, default=5, help="print the N worst clips")

    p = sub.add_parser("status", help="Queue depth, latency and worker state")
    p.add_argument("--hours", type=float, default=1.0, help="stats window")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("cut", help="Split a recorded session into per-transmission clips")
    p.add_argument("session", type=Path)
    p.add_argument("--method", choices=["segmenter", "audio"], default="segmenter",
                   help="segmenter = production M1 segmenter; audio = original M0 cutter")
    p.add_argument("--latency-ms", type=int, default=0)
    p.add_argument("--merge-gap-ms", type=int, default=400)
    p.add_argument("--min-clip-ms", type=int, default=400)

    p = sub.add_parser("draft", help="Draft reference transcripts with a large model")
    p.add_argument("session", type=Path)
    p.add_argument("-n", type=int, default=50)
    p.add_argument("--backend", default="fasterwhisper")
    p.add_argument("--model", default="large-v3-turbo")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--extra", default="", help="backend options, e.g. device=GPU")
    p.add_argument("--min-ms", type=int, default=1500,
                   help="skip clips shorter than this (0 = include short replies)")

    p = sub.add_parser("sample-recent", help="Make a labelling session from recent recordings "
                       "(mix of short replies, unit-ID openers and calls naming --place)")
    p.add_argument("-n", type=int, default=100)
    p.add_argument("--name", default=None, help="session folder name (default <date>-recent)")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--place", action="append", default=[],
                   help="a place name the model mishears, e.g. your county (repeatable)")

    p = sub.add_parser("label", help="Serve a page to correct draft transcripts")
    p.add_argument("session", type=Path)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    p = sub.add_parser("bench", help="Benchmark STT backends on a session's clips")
    p.add_argument("session", type=Path)
    p.add_argument("--refs", type=Path, default=None, help="default: <session>/refs.jsonl")
    p.add_argument("--candidate", action="append", default=None,
                   help="backend:model[:key=val,...] (repeatable); default: built-in list")
    p.add_argument("--threads", default="3", help="comma list, e.g. 1,2,4")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--prompt", default=None, help="initial prompt for backends that support it")

    args = parser.parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.INFO
    cfg = load_config(args.config)
    component = {"run": "capture", "worker": "worker", "web": "web"}.get(args.cmd)
    if component:
        from .logsetup import setup
        setup(component, cfg.storage.data_dir, level)   # console + data/logs/<component>.log
    else:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "probe":
        from .tools import probe
        probe.run(cfg, psi_seconds=args.psi_seconds)
    elif args.cmd == "record":
        from .tools import record
        out = record.run(cfg, duration_s=args.duration)
        print(f"peak level: {record.peak_dbfs(out / 'audio.wav'):.1f} dBFS")
        print(out)
    elif args.cmd == "run":
        from . import app
        if args.replay:
            from .source.replay import ReplaySource
            source = ReplaySource(args.replay, speed=args.speed)
        else:
            from .source.sds200_network import SDS200NetworkSource
            source = SDS200NetworkSource(cfg.scanner, cfg.storage.data_dir / ".rtsp.lock")
        app.run(cfg, source, with_worker=not args.no_worker, drain=args.drain)
    elif args.cmd == "worker":
        import signal
        import threading

        from .worker import run_worker
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        run_worker(cfg, stop=stop, drain=args.drain)
    elif args.cmd == "web":
        from dataclasses import replace

        from .web.server import serve
        web = cfg.web
        if args.bind or args.port:
            web = replace(web, bind=args.bind or web.bind, port=args.port or web.port)
        serve(replace(cfg, web=web))
    elif args.cmd == "retention":
        from . import db, retention
        conn = db.connect(cfg.storage.db_path)
        r = cfg.retention
        print(f"settings: audio {r.audio_days}d, raw scanner data {r.raw_days}d, "
              f"transcripts {r.transcript_days}d, events {r.event_days}d, "
              f"prompt tests keep {r.prompt_tests_keep} (0 = keep forever)")
        rep = retention.run(conn, cfg, dry_run=args.dry_run,
                            vacuum=args.vacuum and not args.dry_run)
        print(rep.summary())
        if rep.missing_files:
            print(f"({rep.missing_files} clip files were already missing)")
        for n in rep.notes:
            print(n)
    elif args.cmd == "clean-start":
        from . import cleanstart, db
        conn = db.connect(cfg.storage.db_path)
        print("Clean start " + cleanstart.preview(conn, cfg).text(dry_run=True) + ".")
        print("Kept: talkgroup settings, prompt/substitution versions, labelled clips and prompt "
              "tests, the admin password. There is no undo.")
        if not args.yes and input("Type DELETE to continue: ").strip() != "DELETE":
            print("Nothing deleted.")
            return 1
        print(cleanstart.run(conn, cfg).text())
    elif args.cmd == "set-password":
        import getpass

        from . import auth, db
        conn = db.connect(cfg.storage.db_path)
        if args.clear:
            auth.set_password(conn, None)
            print("Admin password removed; admin features are off.")
            return 0
        pw = getpass.getpass("New admin password: ")
        if len(pw) < 8:
            print("Use at least 8 characters.")
            return 1
        if getpass.getpass("Again: ") != pw:
            print("They don't match.")
            return 1
        auth.set_password(conn, pw)
        print("Admin password set. Any browsers that were logged in must log in again.")
    elif args.cmd == "evaluate":
        from .tools import evaluate
        evaluate.print_report(evaluate.run(cfg, args.session, show=args.show))
    elif args.cmd == "status":
        from .tools import status
        status.show(cfg, hours=args.hours, as_json=args.json)
    elif args.cmd == "cut":
        from .tools import cut
        if args.method == "segmenter":
            report = cut.run_segmenter(args.session)
        else:
            report = cut.run(args.session, latency_ms=args.latency_ms,
                             merge_gap_ms=args.merge_gap_ms, min_clip_ms=args.min_clip_ms)
        print(report.read_text())
    elif args.cmd == "draft":
        from .tools import draft
        extra = dict(kv.split("=", 1) for kv in args.extra.split(",") if kv)
        print(draft.run(args.session, n=args.n, backend=args.backend, model=args.model,
                        threads=args.threads, extra=extra, min_ms=args.min_ms))
    elif args.cmd == "sample-recent":
        from .tools import sample
        out = sample.run(cfg.storage.db_path, cfg.storage.data_dir, n=args.n, name=args.name,
                         seed=args.seed, places=tuple(args.place))
        print(f"{out}\nnext: draft {out} -n {args.n} --min-ms 0   then: label {out}")
    elif args.cmd == "label":
        from .tools import label
        label.serve(args.session, args.host, args.port)
    elif args.cmd == "bench":
        from . import bench
        specs = []
        for c in args.candidate or []:
            backend, model, *rest = c.split(":")
            extra = dict(kv.split("=", 1) for kv in rest[0].split(",")) if rest else {}
            specs.append((backend, model, extra))
        specs = specs or bench.DEFAULT_CANDIDATES
        cands = [bench.Candidate(b, m, e, int(t))
                 for b, m, e in specs for t in args.threads.split(",")]
        refs = args.refs or args.session / "refs.jsonl"
        bench.run(args.session / "clips", refs, cands, cfg.storage.data_dir / "bench",
                  limit=args.limit, prompt=args.prompt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
