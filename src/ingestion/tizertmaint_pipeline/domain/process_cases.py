"""Printed PFD process cases (with or without backfill) and their stable identifiers."""

from __future__ import annotations

import re
import unicodedata

from .ids import stable_urn

CASES = (
    ("without-backfill", re.compile(r"^with\s*out\s*back")),
    ("with-backfill", re.compile(r"^with\s*back")),
)


def case_key(text: str | None) -> str | None:
    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip().lower()
    return next((key for key, pattern in CASES if pattern.search(normalized)), None)


def process_case_id(key: str) -> str:
    """Same identifier as ``applicability.case_id`` of pfd-observation packets."""

    return stable_urn("process-case", "tizert-pfd", key)
