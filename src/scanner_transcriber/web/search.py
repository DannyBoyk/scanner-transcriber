"""Forgiving transcript search: words, stems, partial words, phrases, exclusions.

Each term matches if EITHER index matches it:
  * transmissions_fts (porter stemming): "stop" finds "stops", "stopped"
  * transmissions_tri (trigram): substrings of 3+ characters, so "Quar" finds "Quarry" and
    "1403" finds "Medic 1403" (transcription misspellings make whole-word search miss a lot)
All terms must match. User input is never handed to FTS5 raw: it's parsed into quoted terms,
so odd syntax can't cause errors or change the query's meaning."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from html import escape

from markupsafe import Markup

from .feed import COLUMNS, decode_cursor

TEXT_COLS = "{transcript alt_transcript}"
_TOKEN = re.compile(r'(-?)"([^"]*)"?|(-?)(\S+)')
_WORD_CHARS = re.compile(r"[^\w'-]+")


@dataclass(frozen=True)
class Term:
    text: str
    phrase: bool = False
    negate: bool = False


def parse(q: str) -> list[Term]:
    terms: list[Term] = []
    for m in _TOKEN.finditer(q or ""):
        if m.group(2) is not None:                        # "quoted phrase"
            words = [w for w in (_WORD_CHARS.sub(" ", m.group(2))).split() if w.strip("'-")]
            if words:
                terms.append(Term(" ".join(words), phrase=len(words) > 1, negate=bool(m.group(1))))
        else:
            neg, raw = m.group(3), m.group(4)
            for w in _WORD_CHARS.sub(" ", raw).split():
                w = w.strip("'-")
                if w:
                    terms.append(Term(w, negate=bool(neg)))
    return terms[:20]


def _fts_string(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _term_sql(t: Term) -> tuple[str, list]:
    parts = ["t.id IN (SELECT rowid FROM transmissions_fts WHERE transmissions_fts MATCH ?)"]
    args: list = [f"{TEXT_COLS} : {_fts_string(t.text)}"]
    if len(t.text) >= 3:  # trigram needs 3+ characters
        parts.append("t.id IN (SELECT rowid FROM transmissions_tri WHERE transmissions_tri MATCH ?)")
        args.append(_fts_string(t.text))
    clause = "(" + " OR ".join(parts) + ")"
    return (f"NOT {clause}" if t.negate else clause), args


@dataclass
class Filters:
    tgs: tuple[str, ...] = ()
    department: str | None = None
    since: str | None = None        # UTC ISO, inclusive
    until: str | None = None        # UTC ISO, exclusive
    has_audio: bool = False
    include_no_speech: bool = False


def where(terms: list[Term], f: Filters) -> tuple[list[str], list]:
    clauses, args = [], []
    positive = [t for t in terms if not t.negate]
    if terms and not positive:
        clauses.append("t.transcript IS NOT NULL")    # only exclusions: search all transcripts
    for t in terms:
        c, a = _term_sql(t)
        clauses.append(c)
        args += a
    if f.tgs:
        clauses.append(f"t.tgid IN ({','.join('?' * len(f.tgs))})")
        args += list(f.tgs)
    if f.department:
        clauses.append("t.department = ?")
        args.append(f.department)
    if f.since:
        clauses.append("t.started_at >= ?")
        args.append(f.since)
    if f.until:
        clauses.append("t.started_at < ?")
        args.append(f.until)
    if f.has_audio:
        clauses.append("t.audio_path IS NOT NULL")
    if not f.include_no_speech:
        clauses.append("COALESCE(t.no_speech, 0) = 0")
    return clauses, args


def run(conn: sqlite3.Connection, q: str, f: Filters, *, before: str | None = None,
        limit: int = 50) -> tuple[list[sqlite3.Row], list[Term]]:
    """Newest first, keyset-paged by (started_at, id)."""
    terms = parse(q)
    clauses, args = where(terms, f)
    if before:
        ts, tx_id = decode_cursor(before)
        clauses.append("(t.started_at, t.id) < (?, ?)")
        args += [ts, tx_id]
    sql = f"SELECT {COLUMNS} FROM transmissions t LEFT JOIN channels c ON c.key = t.channel_key"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY t.started_at DESC, t.id DESC LIMIT ?"
    return conn.execute(sql, (*args, limit)).fetchall(), terms


def count(conn: sqlite3.Connection, q: str, f: Filters) -> int:
    clauses, args = where(parse(q), f)
    sql = "SELECT COUNT(*) FROM transmissions t"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return conn.execute(sql, args).fetchone()[0]


# ---- highlighting --------------------------------------------------------------------------

_SUFFIXES = ("ing", "ed", "es", "s", "ly")


def _stem(w: str) -> str:
    w = w.lower()
    for suf in _SUFFIXES:
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def highlight(text: str | None, terms: list[Term]) -> Markup | None:
    """HTML-escaped `text` with each positive term's matches wrapped in <mark>. Substring
    matches for 3+ characters (like the trigram index); whole words for shorter terms; stems
    so "stops" also marks "stop"."""
    if not text:
        return None
    spans: list[tuple[int, int]] = []
    for t in terms:
        if t.negate:
            continue
        needles = {t.text}
        if not t.phrase:
            needles.add(_stem(t.text))
        for n in needles:
            if len(n) < 3:
                pat = re.compile(rf"(?<!\w){re.escape(n)}(?!\w)", re.IGNORECASE)
            else:
                pat = re.compile(re.escape(n).replace(r"\ ", r"\W+"), re.IGNORECASE)
            spans += [(m.start(), m.end()) for m in pat.finditer(text)]
    if not spans:
        return Markup(escape(text))
    spans.sort()
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out, pos = [], 0
    for a, b in merged:
        out.append(escape(text[pos:a]))
        out.append(f"<mark>{escape(text[a:b])}</mark>")
        pos = b
    out.append(escape(text[pos:]))
    return Markup("".join(out))
