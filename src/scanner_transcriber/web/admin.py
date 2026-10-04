"""Admin page helpers: Whisper token counting, word diffs, prompt-leak hints."""
from __future__ import annotations

import difflib
import functools
import logging
import re

from .. import guards

log = logging.getLogger(__name__)
PROMPT_TOKEN_LIMIT = 223      # Whisper keeps only the last 223 prompt tokens
PROMPT_TOKEN_WARN = 200


@functools.lru_cache(maxsize=4)
def _tokenizer(model: str):
    try:
        from huggingface_hub import snapshot_download
        from tokenizers import Tokenizer

        path = snapshot_download(model, local_files_only=True)
        return Tokenizer.from_file(f"{path}/tokenizer.json")
    except Exception as e:  # noqa: BLE001 — fall back to an estimate
        log.warning("no tokenizer for %s (%s); token counts are estimates", model, e)
        return None


def count_tokens(text: str, model: str) -> tuple[int, bool]:
    """(tokens, exact). Whisper tokenizes the prompt with a leading space."""
    tok = _tokenizer(model)
    if tok is None:
        return max(1, round(len(text) / 3.6)) if text else 0, False
    return (len(tok.encode(" " + text).ids) if text.strip() else 0), True


def token_level(n: int) -> str:
    return "error" if n > PROMPT_TOKEN_LIMIT else "warn" if n >= PROMPT_TOKEN_WARN else "ok"


def word_diff(old: str, new: str) -> list[tuple[str, str]]:
    """[(op, text)] with op in eq | del | ins, word-level."""
    a, b = re.findall(r"\S+\s*", old or ""), re.findall(r"\S+\s*", new or "")
    out: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(("eq", "".join(a[i1:i2])))
        else:
            if i2 > i1:
                out.append(("del", "".join(a[i1:i2])))
            if j2 > j1:
                out.append(("ins", "".join(b[j1:j2])))
    return out


def leak_hints(prompt: str) -> list[str]:
    """Adjacent everyday phrases (lower-case items in a comma list) that together make 4+ words.
    If someone says them back to back, the prompt-leak guard (4 prompt words in order) will
    re-run that clip without the prompt: harmless, but worth knowing."""
    hints = []
    for sentence in re.split(r"[.;]\s*", prompt):
        items = [i.strip() for i in sentence.split(",") if i.strip()]
        for a, b in zip(items, items[1:]):
            if a[:1].islower() and b[:1].islower() and len(guards.words(f"{a} {b}")) >= 4:
                hints.append(f"{a}, {b}")
    return hints
