"""Document types named by the corpus's category folders (``1-Plans``, ``3-Fiches techniques``...).

Classes come from folder labels by meaning, never from their reused ordinal prefix; files with no
category folder fall back to what their name declares: the discipline of an SP0904 engineering
number, the DRS category of a SENET vendor number, or a document word such as "MTO". They are a
retrieval scope, not a reviewed classification of the document's content.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

UNCLASSIFIED = "unclassified"
# Order matters: "8-PMP & Planning lubrification" is lubrication, "7-Modele" is not a procedure.
_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), name)
    for pattern, name in (
        (r"lubrif|\bpmp\b", "lubrication"),
        (r"\bplans?\b|\bdrawings?\b", "plan"),
        (r"\bmod[eè]les?\b|\bmodels?\b", "model"),
        (r"\bmanuels?\b|\bmanuals?\b", "manufacturer_manual"),
        (r"\bfiches?\b|data\s?sheets?", "technical_datasheet"),
        (r"\bbom\b|nomenclature", "bom"),
        (r"\bmodes?\b|op[ée]ratoire", "operating_procedure"),
        (r"pr[ée]vent", "preventive_maintenance"),
        (r"\bs(?:ch|hc|h)[ée]mas?\b", "electrical_instrumentation_schematic"),
        (r"certificat", "certificate"),
        (r"catalogue", "catalogue"),
        (r"calcul", "calculation_note"),
    )
)
DOCUMENT_CLASSES = (*dict.fromkeys(name for _, name in _RULES), UNCLASSIFIED)

# SP0904-<area>-<discipline letter, or a package such as 1I07>-<type>-<sequence>.
_ENGINEERING_NUMBER = re.compile(
    r"(?<![A-Z0-9])SP0904-\d{4}-(?:\d(?P<package>[A-Z])\d{2}|(?P<discipline>[A-Z]))-[A-Z]{4}-\d{3,6}",
    re.IGNORECASE,
)
# Disciplines whose numbers agree with the folder class elsewhere in the corpus (I 99.6%,
# M 99.9%, S 100%; E files never sit in category folders). C (civil) splits 83/17: left out.
_DISCIPLINE_CLASSES = {
    "E": "electrical_instrumentation_schematic",
    "I": "electrical_instrumentation_schematic",
    "M": "plan",
    "S": "plan",
}
# SENET vendor numbers: SP0904-<purchase package>-<DRS category>-<sequence>.
_VENDOR_NUMBER = re.compile(
    r"(?<![A-Z0-9])SP0904-[A-Z]\d{3}\.\d{2}-(?P<category>[A-Z]\d{2})-\d{4}", re.IGNORECASE
)
# Only categories whose title the corpus's vendor registers and cover sheets state. Quality
# records (test plans, welding and NDE qualifications, test reports) are filed with certificates.
_DRS_CLASSES = {
    "G02": "plan",
    "G05": "manufacturer_manual",
    "G15": "bom",
    "G19": "calculation_note",
    **dict.fromkeys(("Q03", "Q06", "Q07", "Q11", "Q13", "Q15", "Q16", "Q17", "Q20"), "certificate"),
}
_NAME_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), name)
    for pattern, name in (
        (
            r"\bmto\b|material take.?off|bill of materials?|\bbom\b|nomenclature"
            r"|\bspare(?:s| parts)? list|\bparts list",
            "bom",
        ),
        (r"data\s?-?sheets?|duty\s?-?sheets?|\bfiches?\s+tech", "technical_datasheet"),
        (r"\bmanuals?\b|\bmanuels?\b", "manufacturer_manual"),
        (r"\bcertificat", "certificate"),
        (r"\bcalculations?\b|\bnotes? de calcul", "calculation_note"),
    )
)


def classify_document_category(category: str | None) -> str:
    if not category:
        return UNCLASSIFIED
    label = unicodedata.normalize("NFKC", category).casefold()
    return next((name for pattern, name in _RULES if pattern.search(label)), UNCLASSIFIED)


def classify_engineering_number(file_name: str) -> str:
    match = _ENGINEERING_NUMBER.search(file_name)
    if match is None:
        return UNCLASSIFIED
    discipline = (match["package"] or match["discipline"]).upper()
    return _DISCIPLINE_CLASSES.get(discipline, UNCLASSIFIED)


def classify_file_name(file_name: str) -> str:
    """Engineering-number discipline, else vendor DRS category, else a document word."""

    if (name := classify_engineering_number(file_name)) != UNCLASSIFIED:
        return name
    vendor = _VENDOR_NUMBER.search(file_name)
    if vendor and (drs := _DRS_CLASSES.get(vendor["category"].upper())):
        return drs
    label = unicodedata.normalize("NFKC", file_name).casefold()
    return next((name for pattern, name in _NAME_RULES if pattern.search(label)), UNCLASSIFIED)


def document_class(folders: Sequence[str], dossier: int | None, file_name: str = "") -> str:
    """Category folder under the equipment dossier; without one, the shallowest category
    folder below the zone; without either, what the file name declares."""

    candidates = folders[dossier + 1 : dossier + 2] if dossier is not None else folders[1:]
    return next(
        (name for name in map(classify_document_category, candidates) if name != UNCLASSIFIED),
        classify_file_name(file_name),
    )
