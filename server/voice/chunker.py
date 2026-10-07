"""Split a token stream into speakable sentences as early as possible.

TTS can't start until it has a complete phrase, so time-to-first-audio is dominated by how
quickly the first sentence closes. We emit on sentence punctuation, and also break long
clauses at a comma once they pass a soft limit, so a rambling first sentence doesn't stall
playback.
"""

from __future__ import annotations

import re

_SENTENCE_END = re.compile(r"([.!?…]+[\"')\]]*)(\s+)")
_ABBREVIATIONS = {"mr.", "mrs.", "ms.", "dr.", "e.g.", "i.e.", "etc.", "vs.", "st.", "no."}


class SentenceChunker:
    def __init__(self, soft_limit: int = 140, min_chars: int = 12):
        self.soft_limit = soft_limit
        self.min_chars = min_chars
        self.buffer = ""

    def push(self, delta: str) -> list[str]:
        self.buffer += delta
        out: list[str] = []
        while True:
            sentence = self._take_sentence() or self._take_long_clause()
            if sentence is None:
                return out
            out.append(sentence)

    def flush(self) -> list[str]:
        rest, self.buffer = _clean(self.buffer), ""
        return [rest] if rest else []

    def _take_sentence(self) -> str | None:
        for m in _SENTENCE_END.finditer(self.buffer):
            candidate = self.buffer[: m.end(1)]
            last_word = candidate.rsplit(None, 1)[-1].lower()
            if last_word in _ABBREVIATIONS or re.search(r"\d\.$", candidate):  # "Dr." / "3.14"
                continue
            if len(candidate.strip()) < self.min_chars:  # merge "Yes." into the next sentence
                continue
            self.buffer = self.buffer[m.end():]
            return _clean(candidate)
        return None

    def _take_long_clause(self) -> str | None:
        if len(self.buffer) < self.soft_limit:
            return None
        cut = self.buffer.rfind(", ", 0, self.soft_limit + 40)
        if cut < self.min_chars:
            return None
        clause, self.buffer = self.buffer[: cut + 1], self.buffer[cut + 2:]
        return _clean(clause)


def _clean(text: str) -> str:
    # Voice output: strip markdown the model may still emit.
    text = re.sub(r"[*_`#>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()
