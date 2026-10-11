"""Milestone 0 benchmark: RTF, memory, thermals and WER/CER per STT backend/model.

Each candidate runs in its own subprocess so peak RSS is per-model and a crash
(e.g. SIGILL on this non-AVX CPU) is recorded instead of killing the run.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import queue
import re
import resource
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .audio import read_wav, resample

DEFAULT_CANDIDATES = [
    ("moonshine", "tiny", {}),
    ("moonshine", "base", {}),
    ("sherpa", "sherpa-onnx-nemo-parakeet_tdt_ctc_110m-en-36000-int8", {}),
    ("onnxasr", "nemo-parakeet-tdt-0.6b-v2", {"quantization": "int8"}),
    ("fasterwhisper", "tiny.en", {}),
    ("fasterwhisper", "base.en", {}),
    ("fasterwhisper", "small.en", {}),
]

_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
          "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
          "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_BREAK = "|"          # punctuation boundary: numbers on either side are not joined


def _small(tokens: list[str], j: int) -> tuple[int | None, int]:
    """A spoken number below 100 starting at tokens[j]: (value, tokens used) or (None, 0)."""
    w = tokens[j] if j < len(tokens) else ""
    nxt = tokens[j + 1] if j + 1 < len(tokens) else ""
    if w in _TENS:
        if nxt in _UNITS and _UNITS[nxt] > 0:
            return _TENS[w] + _UNITS[nxt], 2
        return _TENS[w], 1
    if w in _TEENS:
        return _TEENS[w], 1
    if w in _UNITS:
        return _UNITS[w], 1
    return None, 0


def _numbers(tokens: list[str]) -> list[str]:
    """Spoken numbers → digit strings: "twenty one" → "21", "one hundred (and) six" → "106"."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        v, used = _small(tokens, i)
        if v is None:
            out.append(tokens[i])
            i += 1
            continue
        if used == 1 and v < 10 and i + 1 < len(tokens) and tokens[i + 1] == "hundred":
            v, j = v * 100, i + 2
            k = j + 1 if j < len(tokens) and tokens[j] == "and" else j
            rest, n = _small(tokens, k)
            if rest is not None:
                v, j = v + rest, k + n
            out.append(str(v))
            i = j
            continue
        out.append(str(v))
        i += used
    return out


def normalize(text: str) -> str:
    """Lenient normalization so WER reflects content, not formatting. Each number is one
    token however it's said or written: "135" == "one three five" == "one thirty-five";
    "106" == "one oh six" == "one hundred six"; "21" == "twenty-one". A wrong unit ID is one
    error, not one per digit."""
    t = text.lower()
    t = re.sub(r"\b(10-4|ten-four|ten four)\b", " tenfour ", t)
    t = re.sub(r"(\w)'s\b", r"\1", t)                     # "one oh six's" → "one oh six"
    t = re.sub(r"(?<=\d)[-:](?=\d)", " ", t)               # "19-15", "20:01" → digits, joined below
    t = t.replace("-", " ")
    t = re.sub(r"[^\w\s']", f" {_BREAK} ", t)
    tokens = _numbers(t.split())
    out: list[str] = []
    for w in tokens:                                         # "1 0 6" / "1 35" / "20 20" → one number
        if w.isdigit() and out and out[-1].isdigit():
            out[-1] += w
        else:
            out.append(w)
    return " ".join(w for w in out if w != _BREAK)


def _cpu_temp_c() -> float | None:
    temps = []
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            if "x86_pkg_temp" in (zone / "type").read_text() or not temps:
                temps.append(int((zone / "temp").read_text()) / 1000)
        except OSError:
            continue
    return max(temps) if temps else None


def _cpu_mhz() -> float | None:
    mhz = [float(line.split(":")[1]) for line in Path("/proc/cpuinfo").read_text().splitlines()
           if line.startswith("cpu MHz")]
    return sum(mhz) / len(mhz) if mhz else None


def _cpu_seconds() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime


def _worker(backend: str, model: str, extra: dict, threads: int,
            clips: list[tuple[str, str]], prompt: str | None, q: mp.Queue) -> None:
    from .stt.base import SttConfig, get_backend

    try:
        t0 = time.perf_counter()
        b = get_backend(backend)
        b.load(SttConfig(model=model, threads=threads, prompt=prompt, extra=extra))
        load_s = time.perf_counter() - t0
        # warm-up (first call pays graph init)
        pcm, rate = read_wav(clips[0][1])
        b.transcribe(resample(pcm, rate))
        rows, temps, mhz = [], [], []
        cpu0 = _cpu_seconds()
        for cid, path in clips:
            pcm, rate = read_wav(path)
            x = resample(pcm, rate)
            s = time.perf_counter()
            r = b.transcribe(x)
            elapsed = time.perf_counter() - s
            rows.append({"id": cid, "audio_s": len(pcm) / rate, "stt_s": elapsed,
                         "text": r.text, "confidence": r.confidence, "no_speech": r.no_speech})
            temps.append(_cpu_temp_c())
            mhz.append(_cpu_mhz())
        q.put({"ok": True, "load_s": load_s, "rows": rows, "cpu_s": _cpu_seconds() - cpu0,
               "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024,
               "temp_c": [t for t in temps if t is not None],
               "mhz": [m for m in mhz if m is not None]})
    except Exception as e:  # noqa: BLE001 — report any failure as a result row
        q.put({"ok": False, "error": f"{type(e).__name__}: {e}"})


@dataclass
class Candidate:
    backend: str
    model: str
    extra: dict
    threads: int

    @property
    def label(self) -> str:
        q = self.extra.get("quantization") or self.extra.get("compute_type") or ""
        return f"{self.backend}:{self.model}{'/' + q if q else ''} t{self.threads}"


def run(clips_dir: Path, refs_path: Path | None, candidates: list[Candidate],
        out_root: Path, *, limit: int | None = None, prompt: str | None = None) -> Path:
    import jiwer

    clip_meta = [json.loads(line) for line in (clips_dir.parent / "clips.jsonl").open()]
    refs: dict[str, str] = {}
    if refs_path and refs_path.exists():
        for line in refs_path.open():
            r = json.loads(line)
            if r.get("text") is not None and not r.get("skip"):
                refs[r["id"]] = r["text"]
    ids = [c["id"] for c in clip_meta if not refs or c["id"] in refs]
    if limit:
        ids = ids[:limit]
    clips = [(cid, str(clips_dir / f"{cid}.wav")) for cid in ids]

    out_dir = out_root / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    ctx = mp.get_context("spawn")
    for cand in candidates:
        print(f"== {cand.label} on {len(clips)} clips", flush=True)
        q = ctx.Queue()
        p = ctx.Process(target=_worker, args=(cand.backend, cand.model, cand.extra,
                                              cand.threads, clips, prompt, q))
        p.start()
        result = None
        while p.is_alive() or not q.empty():
            try:
                result = q.get(timeout=1)
                break
            except queue.Empty:
                continue
        p.join()
        if result is None:
            result = {"ok": False, "error": f"worker exited with code {p.exitcode}"}
        row = {"candidate": cand.label, "backend": cand.backend, "model": cand.model,
               "threads": cand.threads, "ok": result["ok"]}
        if not result["ok"]:
            row["error"] = result["error"]
            print(f"   FAILED: {row['error']}", flush=True)
            summary.append(row)
            continue
        rows = result["rows"]
        audio = sum(r["audio_s"] for r in rows)
        stt = sum(r["stt_s"] for r in rows)
        row.update({
            "clips": len(rows), "audio_s": round(audio, 1), "stt_s": round(stt, 1),
            "rtf": round(stt / audio, 3),
            # CPU-seconds per audio-second summed over threads; low for iGPU backends
            "cpu_rtf": round(result["cpu_s"] / audio, 3),
            "load_s": round(result["load_s"], 1),
            "peak_rss_mb": round(result["peak_rss_mb"]),
            "temp_c_max": max(result["temp_c"], default=None),
            "mhz_mean": round(sum(result["mhz"]) / len(result["mhz"])) if result["mhz"] else None,
        })
        scored = [r for r in rows if r["id"] in refs]
        if scored:
            ref_n = [normalize(refs[r["id"]]) or "<empty>" for r in scored]
            hyp_n = [normalize(r["text"]) or "<empty>" for r in scored]
            speech = [(r_, h_) for r_, h_ in zip(ref_n, hyp_n) if r_ != "<empty>"]
            if speech:
                row["wer"] = round(jiwer.wer([r_ for r_, _ in speech], [h_ for _, h_ in speech]), 3)
                row["cer"] = round(jiwer.cer([r_ for r_, _ in speech], [h_ for _, h_ in speech]), 3)
            silent = [h_ for r_, h_ in zip(ref_n, hyp_n) if r_ == "<empty>"]
            if silent:
                # share of no-speech clips where the model produced any words
                row["halluc"] = round(sum(h_ != "<empty>" for h_ in silent) / len(silent), 2)
        print(f"   RTF={row['rtf']} WER={row.get('wer')} RSS={row['peak_rss_mb']}MB", flush=True)
        summary.append(row)
        safe = re.sub(r"[^\w.-]+", "_", cand.label)
        with (out_dir / f"{safe}.jsonl").open("w") as f:
            for r in rows:
                f.write(json.dumps({**r, "ref": refs.get(r["id"])}) + "\n")

    (out_dir / "results.json").write_text(json.dumps(summary, indent=2))
    cols = ["candidate", "rtf", "cpu_rtf", "wer", "cer", "halluc", "peak_rss_mb", "load_s", "temp_c_max", "mhz_mean"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for r in summary:
        if r["ok"]:
            lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
        else:
            lines.append(f"| {r['candidate']} | FAILED: {r['error']} |" + " |" * (len(cols) - 2))
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return out_dir
