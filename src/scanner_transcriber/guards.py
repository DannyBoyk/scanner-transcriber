"""Post-processing guards for Whisper output: hallucinations, prompt leaks, repeats,
and the user's substitution map. Pure functions; the worker decides what to do with them."""
from __future__ import annotations

import re

# Whole-output phrases Whisper is known to produce on silence, noise or faint audio.
# Seen on this scanner: "Thank you.", "Thank you. Thank you.", "...", "Hmm."
# Only phrases unlikely to be real radio traffic on their own: "Okay", "Copy", "Go ahead"
# are common genuine replies and must never be treated as filler.
HALLUCINATIONS = {
    "thank you", "thank you very much", "thanks for watching", "thank you for watching",
    "thank you so much", "please subscribe", "bye bye", "you", "hmm", "um", "uh",
}

_WORD = re.compile(r"[a-z0-9']+")


def words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def is_blank(text: str) -> bool:
    """No words at all (empty, whitespace, or only punctuation like '...')."""
    return not words(text)


def is_hallucination(text: str) -> bool:
    """The whole output is a known filler phrase, possibly repeated ('Thank you. Thank you.')."""
    w = words(text)
    if not w:
        return False
    for n in range(1, min(len(w), 4) + 1):
        unit = " ".join(w[:n])
        if unit in HALLUCINATIONS and len(w) % n == 0 and w == w[:n] * (len(w) // n):
            return True
    return False


def collapse_repeats(text: str, min_repeats: int = 3) -> tuple[str, bool]:
    """Collapse a phrase of 1-6 words repeated `min_repeats`+ times in a row to one copy.

    Works on the original text (keeping case/punctuation of the first copy)."""
    tokens = text.split()
    norm = [re.sub(r"[^\w']", "", t.lower()) for t in tokens]
    changed = False
    i = 0
    out: list[str] = []
    while i < len(tokens):
        best = None
        for n in range(6, 0, -1):
            if i + n * min_repeats > len(tokens):
                continue
            unit = norm[i:i + n]
            if not any(unit):
                continue
            reps = 1
            while norm[i + reps * n:i + (reps + 1) * n] == unit:
                reps += 1
            if reps >= min_repeats:
                best = (n, reps)
                break
        if best:
            n, reps = best
            out.extend(tokens[i:i + n])
            i += n * reps
            changed = True
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out), changed


def prompt_leak(text: str, prompt: str, min_run: int = 4) -> bool:
    """True if the output contains `min_run`+ consecutive words in the same order as the
    prompt, e.g. "Alpha Bravo Charlie Delta" or "Ashford Brookfield Carver Dunmore".

    Deliberately ignores scattered prompt words: plates like "Sierra Zulu Tango" and place
    names are legitimately said on the radio."""
    if not prompt:
        return False
    p = words(prompt)
    t = words(text)
    grams = {tuple(p[i:i + min_run]) for i in range(len(p) - min_run + 1)}
    return any(tuple(t[i:i + min_run]) in grams for i in range(len(t) - min_run + 1))


def substitute(text: str, subs: dict[str, str]) -> str:
    """Case-insensitive whole-word/phrase replacements (Admin page). Both sides are literal
    text: a backslash in the replacement is just a backslash, not a regex group reference."""
    for src, dst in subs.items():
        text = re.sub(rf"(?<![\w']){re.escape(src)}(?![\w'])", lambda _m, d=dst: d, text,
                      flags=re.IGNORECASE)
    return text
