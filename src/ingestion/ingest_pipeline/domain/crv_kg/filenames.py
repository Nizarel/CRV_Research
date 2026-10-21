"""Facts printed in CRV file names: genre, MM-YYYY date, revision, copy and chapter markers.

Measured on the corpus (1,967 files): 1,617 names end with ``MM-YYYY``; the first word is the
genre (rapport, pv, etude, executive, synthese...); EIE chapters carry ``_<ddmmyyyy>_R<n>``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import PurePosixPath

from .vocab import GENRE_WORDS, MONTHS, fold, periodic_genre

_MONTH_YEAR = re.compile(r"[-_ ]\s*(\d{1,2})\s*-\s*((?:19|20)\d{2})\s*-?\s*$")
_EIE_REVISION = re.compile(r"[_ ](\d{2})(\d{2})((?:19|20)\d{2})[_ ]R(\d{1,2})\s*$", re.I)
_EIE_REVISION_FIRST = re.compile(r"[_ ]R(\d{1,2})[_ ](\d{2})(\d{2})((?:19|20)\d{2})\s*$", re.I)
_MONTH_NAME_YEAR = re.compile(
    r"\b(" + "|".join(sorted(MONTHS, key=len, reverse=True)) + r")\s*((?:19|20)\d{2})\b"
)
_QUARTER_YEAR = re.compile(r"\bt([1-4])\s*((?:19|20)\d{2})\b")
_DAY_MONTH_YY = re.compile(r"(?<!\d)(\d{2})[-_. ]?(\d{2})[-_. ]?(\d{2})(?!\d)")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_REV_WORD = re.compile(r"\bR[ée]v(?:ision)?\.?\s*(\d{1,2})\b", re.I)
_REV_SUFFIX = re.compile(r"[_ -]R(\d{1,2})\s*$", re.I)
_VERSION = re.compile(r"(?:^|[_ -])V(\d{1,2})(?:$|[_ -])", re.I)
_STATE = re.compile(r"\b(provisoire|draft|final|finale|definitif|definitive)\b")
_COPY = re.compile(r"\(\d\)\s*$|\b(copie|copy)\b", re.I)
_CHAPTER = re.compile(r"(?:^|\b)(?:\d+\s*[._ ]\s*)?(?:chapitre|chapter)\s*(\d+)", re.I)
_ANNEX = re.compile(r"\bannex(?:e|ure)?\s*[_ -]?\s*([A-Z]|\d{1,2})(?=$|[\s_.-])", re.I)
_STATE_RANK = {"draft": 0, "provisoire": 0, "final": 9, "finale": 9, "definitif": 9,
               "definitive": 9}


@dataclass(frozen=True)
class FileNameFacts:
    file_name: str
    title: str
    """Stem without the date, revision and copy suffixes, as printed."""
    family_key: str
    """Folded title plus its MM-YYYY: versions and copies of one document share it, while
    successive periodic reports with the same title do not."""
    genre: str | None
    genre_word: str | None
    year: int | None
    month: int | None
    date_kind: str | None
    """``month_year`` (``-05-2014-``), ``month_name`` (``Mai 2017``), ``quarter`` (``T4_2014``)
    or ``year`` (a lone 4-digit year: weakest)."""
    revision: str | None
    revision_rank: int | None
    revision_date: date | None
    copy: bool
    part: tuple[str, str] | None
    """``("chapter", "10")`` or ``("annex", "U")``."""


def parse_file_name(path: str) -> FileNameFacts:
    name = PurePosixPath(path.replace("\\", "/")).name
    stem = name.rsplit(".", 1)[0] if "." in name else name
    title = stem.strip()
    year = month = None
    date_kind: str | None = None
    revision: str | None = None
    rank: int | None = None
    revision_date: date | None = None

    copy = bool(_COPY.search(title))
    title = _COPY.sub("", title).strip()
    if match := _EIE_REVISION.search(title):
        day, mon, yr, number = (int(group) for group in match.groups())
        revision, rank = f"R{number}", number
        revision_date = _date(yr, mon, day)
        title = title[: match.start()].strip(" _-")
    elif match := _EIE_REVISION_FIRST.search(title):
        number, day, mon, yr = (int(group) for group in match.groups())
        revision, rank = f"R{number}", number
        revision_date = _date(yr, mon, day)
        title = title[: match.start()].strip(" _-")
    if match := _MONTH_YEAR.search(title):
        candidate_month, candidate_year = int(match.group(1)), int(match.group(2))
        if 1 <= candidate_month <= 12:
            month, year, date_kind = candidate_month, candidate_year, "month_year"
            title = title[: match.start()].strip(" _-")
    if revision is None and (match := _REV_SUFFIX.search(title)):
        revision, rank = f"R{int(match.group(1))}", int(match.group(1))
        title = title[: match.start()].strip(" _-")
    if revision is None and (match := _REV_WORD.search(title)):
        revision, rank = f"Rév.{int(match.group(1))}", int(match.group(1))
    if revision is None and (match := _VERSION.search(title)):
        revision, rank = f"V{int(match.group(1))}", int(match.group(1))
    folded = fold(title)
    if revision is None and (match := _STATE.search(folded)):
        revision, rank = match.group(1), _STATE_RANK[match.group(1)]
    if year is None and (match := _MONTH_NAME_YEAR.search(folded)):
        month, year, date_kind = MONTHS[match.group(1)], int(match.group(2)), "month_name"
    elif year is None and (match := _QUARTER_YEAR.search(folded)):
        year, date_kind = int(match.group(2)), "quarter"
    elif year is None and (years := _YEAR.findall(folded)):
        year, date_kind = int(years[-1]), "year"
    elif year is None and (short := _short_date(title)):
        year, month, date_kind = short.year, short.month, "day_month_yy"

    words = [word for word in folded.split() if not word.isdigit()]
    genre_word = words[0] if words else None
    genre = periodic_genre(folded) or GENRE_WORDS.get(genre_word or "")
    part: tuple[str, str] | None = None
    if match := _CHAPTER.search(title):
        part = ("chapter", str(int(match.group(1))))
    elif match := _ANNEX.search(title):
        part = ("annex", match.group(1).upper())
    if part and genre is None:
        genre = "chapter" if part[0] == "chapter" else "annex"

    key_title = _STATE.sub(" ", folded)
    key_title = _REV_WORD.sub(" ", key_title)
    family_key = " ".join(key_title.split())
    if date_kind == "month_year" and year is not None and month is not None:
        family_key = f"{family_key} {month:02d}-{year}"
    return FileNameFacts(
        file_name=name,
        title=title,
        family_key=family_key,
        genre=genre,
        genre_word=genre_word,
        year=year,
        month=month,
        date_kind=date_kind,
        revision=revision,
        revision_rank=rank,
        revision_date=revision_date,
        copy=copy,
        part=part,
    )


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _short_date(title: str) -> date | None:
    """``13-11-14`` or ``260724`` (day, month, two-digit year) in a presentation name."""

    for match in _DAY_MONTH_YY.finditer(title):
        day, month, yy = (int(group) for group in match.groups())
        if found := _date(2000 + yy if yy < 50 else 1900 + yy, month, day):
            return found
    return None
