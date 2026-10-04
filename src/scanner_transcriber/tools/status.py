"""`status` command: the same health evaluation the web header dot and /status page use."""
from __future__ import annotations

import json
from datetime import datetime

from .. import db, health, jobs, statestore
from ..config import Config

ICON = {"ok": "OK   ", "warn": "WARN ", "error": "ERROR"}


def _local(ts: str | None) -> str:
    return datetime.fromisoformat(ts).astimezone().strftime("%b %d %H:%M:%S") if ts else "-"


def show(cfg: Config, *, hours: float = 1.0, as_json: bool = False, events: int = 8) -> None:
    conn = db.connect(cfg.storage.db_path)
    h = health.evaluate(conn, cfg)
    s = jobs.stats(conn, window_hours=hours)
    ev = statestore.recent_events(conn, limit=events)
    conn.close()
    if as_json:
        print(json.dumps({"health": h.as_dict(), "transcription": s, "events": ev}, indent=2,
                         default=str))
        return

    print(f"overall:  {ICON[h.level].strip()}")
    for c in h.conditions:
        print(f"  {ICON[c.level]} [{c.component}] {c.message}")

    cap = h.components.get("capture")
    if cap:
        d = cap["detail"]
        line = f"capture:  {cap['state']} ({d.get('source', '?')})"
        if d.get("connected_since"):
            line += f", connected since {_local(d['connected_since'])}"
        if d.get("reconnects"):
            line += f", {d['reconnects']} reconnect(s)"
        if d.get("rtp_packets_lost"):
            line += f", {d['rtp_packets_lost']} RTP packets lost"
        print(line)
        cur = d.get("current")
        if cur:
            print(f"          hearing TG {cur['tgid']} {cur.get('department')}/{cur.get('channel')}")
    w = h.components.get("worker")
    if w:
        d = w["detail"]
        print(f"worker:   {w['state']} ({d.get('model')}), {d.get('processed', 0)} clips this run")

    print("queue:    " + (", ".join(f"{k}={v}" for k, v in sorted(s["by_status"].items()))
                          or "empty"))
    if s["done_last_window"]:
        print(f"last {hours:g}h: {s['done_last_window']} transcribed; latency p50 "
              f"{s['latency_s_p50']}s / p95 {s['latency_s_p95']}s; "
              f"flagged {s['flagged_share']:.0%}; no speech {s['no_speech_share']:.0%}")
    host = h.host
    print(f"host:     disk {host['disk_free_gb']:.1f} GB free ({host['disk_free_pct']:.0f}%)"
          + (f", CPU {host['cpu_temp_c']:.0f}°C" if host.get("cpu_temp_c") else "")
          + (f" @ {host['cpu_mhz']} MHz" if host.get("cpu_mhz") else ""))
    if ev:
        print("recent events:")
        for e in ev:
            print(f"  {_local(e['at'])}  {e['level']:<5} [{e['component']}] {e['message']}")
