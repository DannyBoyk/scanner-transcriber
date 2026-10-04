"""Soak sampler (M5 phase D #8/#9): every INTERVAL seconds, append one CSV row to
data/soak/soak-<start>.csv with temperature, throttling, memory per process, queue latency,
disk and reconnects. Read-only against the database. Run it detached, e.g.:

    systemd-run --user --unit=st-soak .venv/bin/python deploy/soak.py --hours 72

then summarise with `deploy/soak.py --report data/soak/soak-<start>.csv`."""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sqlite3
import statistics
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"
DB = DATA / "scanner.db"
FIELDS = ["at", "cpu_temp_c", "cpu_mhz", "throttle_count", "load1", "mem_avail_mb",
          "rss_run_mb", "rss_worker_mb", "rss_web_mb", "queued", "oldest_queued_s",
          "done_5min", "worker_busy_pct", "latency_p50_s", "latency_p95_s", "rtf_mean", "failed_1h",
          "db_mb", "wal_mb", "disk_free_gb", "capture_state", "reconnects", "rtp_lost",
          "worker_mode"]


def _read(path: str, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def _pids(unit: str) -> list[int]:
    out = subprocess.run(["systemctl", "show", "-p", "ControlGroup", "--value", unit],
                         capture_output=True, text=True).stdout.strip()
    procs = _read(f"/sys/fs/cgroup{out}/cgroup.procs", "") if out else ""
    return [int(p) for p in procs.split()]


def _rss_mb(pid: int) -> float:
    for line in (_read(f"/proc/{pid}/status", "") or "").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def _is_worker(pid: int) -> bool:
    return "spawn_main" in (_read(f"/proc/{pid}/cmdline", "") or "").replace("\0", " ")


def sample() -> dict:
    now = datetime.now(UTC)
    temps = [int(t) / 1000 for z in Path("/sys/class/thermal").glob("thermal_zone*")
             if (t := _read(z / "temp"))]
    mhz = [float(line.split(":")[1]) for line in (_read("/proc/cpuinfo", "") or "").splitlines()
           if line.startswith("cpu MHz")]
    throttle = sum(int(_read(p, "0") or 0) for p in
                   map(str, Path("/sys/devices/system/cpu").glob("cpu*/thermal_throttle/core_throttle_count")))
    mem = {line.split(":")[0]: int(line.split()[1]) for line in
           (_read("/proc/meminfo", "") or "").splitlines() if line.split()}
    cap = _pids("scanner-transcriber.service")
    web = _pids("scanner-transcriber-web.service")
    row = {
        "at": now.isoformat(timespec="seconds"),
        "cpu_temp_c": max(temps) if temps else None,
        "cpu_mhz": round(sum(mhz) / len(mhz)) if mhz else None,
        "throttle_count": throttle,
        "load1": os.getloadavg()[0],
        "mem_avail_mb": mem.get("MemAvailable", 0) // 1024,
        "rss_run_mb": round(sum(_rss_mb(p) for p in cap if not _is_worker(p)), 1),
        "rss_worker_mb": round(sum(_rss_mb(p) for p in cap if _is_worker(p)), 1),
        "rss_web_mb": round(sum(_rss_mb(p) for p in web), 1),
        "db_mb": round(DB.stat().st_size / 1e6, 1) if DB.exists() else None,
        "wal_mb": round(Path(f"{DB}-wal").stat().st_size / 1e6, 1) if Path(f"{DB}-wal").exists() else 0,
        "disk_free_gb": round(shutil.disk_usage(DATA).free / 1e9, 1),
    }
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
    try:
        since5 = (now - timedelta(minutes=5)).isoformat(timespec="milliseconds")
        since1h = (now - timedelta(hours=1)).isoformat(timespec="milliseconds")
        row["queued"] = c.execute("SELECT COUNT(*) FROM transmissions WHERE status IN "
                                  "('queued','processing') AND priority >= 0").fetchone()[0]
        oldest = c.execute("SELECT MIN(ended_at) FROM transmissions WHERE status = 'queued' "
                           "AND priority >= 0").fetchone()[0]
        row["oldest_queued_s"] = (round((now - datetime.fromisoformat(oldest)).total_seconds())
                                  if oldest else 0)
        done = c.execute("SELECT ended_at, transcribed_at, stt_ms, duration_ms FROM transmissions "
                         "WHERE +status = 'done' AND transcribed_at >= ?", (since5,)).fetchall()
        lat = sorted((datetime.fromisoformat(t) - datetime.fromisoformat(e)).total_seconds()
                     for e, t, _, _ in done)
        row["done_5min"] = len(done)
        row["latency_p50_s"] = round(lat[len(lat) // 2], 1) if lat else None
        row["latency_p95_s"] = round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 1) if lat else None
        rtf = [s / d for _, _, s, d in done if s and d]
        row["rtf_mean"] = round(sum(rtf) / len(rtf), 2) if rtf else None
        # capacity: share of the last 5 min the worker spent transcribing (100% = at its limit)
        row["worker_busy_pct"] = round(sum(s or 0 for _, _, s, _ in done) / 3000, 1)
        row["failed_1h"] = c.execute("SELECT COUNT(*) FROM transmissions WHERE status = 'failed' "
                                     "AND updated_at >= ?", (since1h,)).fetchone()[0]
        states = {r[0]: (r[1], json.loads(r[2])) for r in
                  c.execute("SELECT name, state, detail FROM component_state")}
    finally:
        c.close()
    cs, cd = states.get("capture", (None, {}))
    ws, wd = states.get("worker", (None, {}))
    row.update(capture_state=cs, reconnects=cd.get("reconnects"), rtp_lost=cd.get("rtp_packets_lost"),
               worker_mode=ws)
    return row


def run(hours: float, interval: float) -> None:
    out_dir = DATA / "soak"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"soak-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    end = time.monotonic() + hours * 3600
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        while time.monotonic() < end:
            try:
                w.writerow(sample())
                f.flush()
            except Exception as e:  # noqa: BLE001 — keep sampling
                print(f"sample failed: {type(e).__name__}: {e}", flush=True)
            time.sleep(interval)


def report(path: Path) -> None:
    rows = list(csv.DictReader(path.open()))
    if not rows:
        print("no samples"); return

    def col(k):
        return [float(r[k]) for r in rows if r.get(k) not in (None, "", "None")]

    def span(k, unit=""):
        v = col(k)
        return f"min {min(v):g}{unit}, median {statistics.median(v):g}{unit}, max {max(v):g}{unit}" if v else "n/a"

    print(f"{path.name}: {len(rows)} samples, {rows[0]['at']} → {rows[-1]['at']}")
    for k, u in [("cpu_temp_c", " °C"), ("cpu_mhz", " MHz"), ("load1", ""), ("mem_avail_mb", " MB"),
                 ("rss_run_mb", " MB"), ("rss_worker_mb", " MB"), ("rss_web_mb", " MB"),
                 ("queued", ""), ("oldest_queued_s", " s"), ("latency_p50_s", " s"),
                 ("latency_p95_s", " s"), ("rtf_mean", ""), ("done_5min", ""), ("worker_busy_pct", " %")]:
        print(f"  {k:16} {span(k, u)}")
    for k in ("rss_run_mb", "rss_worker_mb", "rss_web_mb", "db_mb", "disk_free_gb",
              "throttle_count", "reconnects", "rtp_lost"):
        v = col(k)
        if v:
            print(f"  {k:16} first {v[0]:g} → last {v[-1]:g} (change {v[-1] - v[0]:+g})")
    print(f"  failed clips/h   max {max(col('failed_1h') or [0]):g}")
    print(f"  capture states   {sorted({r['capture_state'] for r in rows})}")
    print(f"  worker modes     {sorted({r['worker_mode'] for r in rows})}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--interval", type=float, default=300)
    ap.add_argument("--once", action="store_true", help="print one sample and exit")
    ap.add_argument("--report", type=Path)
    a = ap.parse_args()
    if a.report:
        report(a.report)
    elif a.once:
        print(json.dumps(sample(), indent=1))
    else:
        run(a.hours, a.interval)
