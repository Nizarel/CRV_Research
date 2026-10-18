"""Token counters: exact tiktoken when available, otherwise a recorded approximation."""

from __future__ import annotations

import math
from typing import Any

EMBEDDING_ENCODING = "cl100k_base"
"""Encoding used by text-embedding-3-large."""


class TiktokenCounter:
    def __init__(self, encoding: Any) -> None:
        self._encoding = encoding

    @property
    def name(self) -> str:
        return f"tiktoken:{self._encoding.name}"

    def count(self, text: str) -> int:
        return len(self._encoding.encode(text, disallowed_special=()))


class ApproximateCounter:
    """~3.2 UTF-8 bytes per token for French/English technical text; never below 1."""

    BYTES_PER_TOKEN = 3.2

    @property
    def name(self) -> str:
        return "approx:utf8/3.2"

    def count(self, text: str) -> int:
        return max(1, math.ceil(len(text.encode("utf-8")) / self.BYTES_PER_TOKEN))


def load_token_counter(encoding: str = EMBEDDING_ENCODING) -> TiktokenCounter | ApproximateCounter:
    """tiktoken needs its BPE file (baked into the image); fall back when it cannot load."""

    try:
        import tiktoken

        return TiktokenCounter(tiktoken.get_encoding(encoding))
    except Exception:
        return ApproximateCounter()
