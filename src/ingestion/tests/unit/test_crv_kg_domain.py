"""KG-1 domain: file-name, report-code and cover parsers, gazetteer, build and projection.

Examples are real names and cover text from the CRV corpus.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ingest_pipeline.domain.crv_kg.build import DocumentInput, Placement, build_run, document_iri
from ingest_pipeline.domain.crv_kg.codes import codes_in, parse_code
from ingest_pipeline.domain.crv_kg.cover import CoverLine, parse_cover
from ingest_pipeline.domain.crv_kg.filenames import parse_file_name
from ingest_pipeline.domain.crv_kg.gazetteer import parse_gazetteer, review_rows
from ingest_pipeline.domain.crv_kg.model import (
    ProjectedAssertion,
    ProjectedEntity,
    build_projection,
)
from ingest_pipeline.domain.crv_kg.vocab import DEFAULT_RELEASE_STATUSES, fold
from ingest_pipeline.domain.errors import InvalidInputError

PACKAGED = Path(__file__).resolve().parents[2] / "ingest_pipeline" / "data" / "crv-gazetteer.json"
TODAY = date(2026, 10, 21)
ROOT = "AI for Science_ Geology & Chemistry"
GED = f"{ROOT}/Augmentation AI Processus R&D/GED_ A alimenter"
BLEIDA = f"{GED}/Documents Activité/BLEIDA"
EIE = f"{ROOT}/Phytoremediation/Données EIE et autres/EIE Tizert"
COVER = "\n".join(
    [
        "CR/AA/RP/001/24",
        "Reminex site de Guemassa - BP. 469 - Marrakech Medina-Maroc",
        "RAPPORT",
        "Emetteur : Centre de recherche Activité Analyses & Caractérisation",
        "Projet : Echantillons poudres SONASID",
        "Titre : Caractérisation des échantillons poudres SONASID",
        "Objet : Caractérisation minéralogique des échantillons poudres de SONASID",
        "Domaine d\u2019application :",
        "Révision Rédaction Vérification Approbation",
        "0",
        "M.BADRI",
        "Date : 10/01/2024",
        "K.OUZAOUIT",
        "Date : 12/01/2024",
        "Attribué à : M ALANSARI Abdelkhalek / SONASID",
        "Copié à :",
    ]
)


def _lines(text: str, page: int = 1) -> tuple[CoverLine, ...]:
    return tuple(
        CoverLine(line, page, f"ev:{page}:{number}")
        for number, line in enumerate(text.splitlines())
    )


def test_file_names_carry_genre_date_revision_and_parts() -> None:
    pilot = parse_file_name(
        f"{BLEIDA}/Rapport de pilotage de flottation du minerai de Jbel Laasel- 05-2014-.pdf"
    )
    assert (pilot.genre, pilot.year, pilot.month, pilot.date_kind) == (
        "report", 2014, 5, "month_year"
    )
    assert pilot.title == "Rapport de pilotage de flottation du minerai de Jbel Laasel"
    assert pilot.family_key.endswith("05-2014")

    chapter = parse_file_name(f"{EIE}/Chapter 10_E Impact Assessment_14122021_R2.pdf")
    assert (chapter.revision, chapter.revision_rank) == ("R2", 2)
    assert chapter.revision_date == date(2021, 12, 14)
    assert chapter.part == ("chapter", "10")
    trik = parse_file_name("EIE Trik/Chapter 13_E_Stakeholder consultation_R0_24072018.pdf")
    assert (trik.revision, trik.revision_date) == ("R0", date(2018, 7, 24))

    annex = parse_file_name(f"{EIE}/Annexure U_Geochemical Report.pdf")
    assert annex.part == ("annex", "U")
    assert annex.genre == "annex"
    monthly = parse_file_name("MAA/Rapports/Reporting_Projet_TIZERT-Mai 2017.pdf")
    assert (monthly.year, monthly.month, monthly.date_kind) == (2017, 5, "month_name")
    assert parse_file_name("Documents Veille/Veille Aboulaich T4_2014-.pdf").year == 2014
    meeting = parse_file_name("SOMIFER/PPT/Réunion_Bleida_27-06-14.ppt")
    assert (meeting.genre, meeting.year, meeting.month) == ("minutes", 2014, 6)
    monthly = parse_file_name("GED/Rapport d'activité du mois de mars 2017- 04-2017-.pdf")
    assert (monthly.genre, monthly.year) == ("activity-report", 2017)
    copy = parse_file_name("CMO/Rapports/CR-AR-CJ-RP-08-11 (2).doc")
    assert copy.copy
    assert copy.title == "CR-AR-CJ-RP-08-11"


def test_report_codes_split_into_segments() -> None:
    full = parse_code("CR/AR/MC/RP/24/10")
    assert full is not None
    assert (full.s2, full.s3, full.doc_type, full.seq, full.yy, full.year) == (
        "AR", "MC", "RP", "24", 10, 2010
    )
    short = parse_code("CR/AA/RP/001/24")
    assert short is not None
    assert (short.s3, short.doc_type, short.seq, short.year) == (None, "RP", "001", 2024)
    procedure = parse_code("CR-AR-I-01.00-05")
    assert procedure is not None
    assert (procedure.code, procedure.doc_type) == ("CR/AR/I/01.00/05", "I")
    assert parse_code("CR-2") is None
    # The running-header OCR misread is repaired before parsing.
    assert [code.code for code in codes_in("CRIAR/PE/RP/09/25 Rapport")] == ["CR/AR/PE/RP/09/25"]


def test_cover_fields_keep_printed_values_and_never_people() -> None:
    cover = parse_cover(_lines(COVER))
    issuer = cover.fields["issuer"].value
    assert issuer == "Centre de recherche Activité Analyses & Caractérisation"
    assert cover.fields["project"].value == "Echantillons poudres SONASID"
    assert cover.fields["title"].value == "Caractérisation des échantillons poudres SONASID"
    assert "scope" not in cover.fields  # empty field; the next line is the revision header
    assert cover.revision is not None
    assert cover.revision.value == "0"
    assert cover.latest_date is not None
    assert cover.latest_date[0] == date(2024, 1, 12)
    stored = json.dumps({key: value.value for key, value in cover.fields.items()})
    assert "BADRI" not in stored
    assert "ALANSARI" not in stored
    assert cover.attributed_to is not None  # resolved to organisations, never stored
    assert parse_cover(_lines("Projet de valorisation du cuivre de Tizert en 2015")).fields == {}
    eie_cover = "Tizert Copper Project\nEnvironmental Impact Assessment\nDécembre 2021"
    eie = parse_cover(_lines(eie_cover))
    assert eie.period is not None
    assert eie.period[:2] == (2021, 12)


def test_packaged_gazetteer_is_valid_and_provisional() -> None:
    gazetteer = parse_gazetteer(PACKAGED.read_bytes())
    assert {entry.status for entry in gazetteer.entries} == {"candidate"}
    tizert = gazetteer.for_code("tz")
    assert tizert is not None
    assert tizert.iri == "urn:crv:site:tizert"
    folders = gazetteer.for_folders(["Documents Activité", "AKKA - AGM"])
    assert [entry.iri for entry in folders] == ["urn:crv:site:akka"]
    mentioned = gazetteer.mentioned("flottation du cuivre de Ouanssimi", kind="Site")
    assert [entry.iri for entry in mentioned] == ["urn:crv:site:ouansimi"]
    rows = review_rows(gazetteer)
    assert {"expert_decision", "report_code_segment3"} <= set(rows[0])


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"iri": "urn:tizert:site:x"}, "iri must be"),
        ({"status": "approved"}, "decision_id"),
        ({"codes": ["TZ"]}, "two gazetteer entries"),
        ({"operated_by": "urn:crv:org:nobody"}, "unknown"),
    ],
)
def test_gazetteer_defects_reject_the_file(change: dict[str, object], message: str) -> None:
    base = {"iri": "urn:crv:site:x", "kind": "Site", "label": "X"}
    entries = [
        {"iri": "urn:crv:site:tizert", "kind": "Site", "label": "Tizert", "codes": ["TZ"]},
        {**base, **change},
    ]
    with pytest.raises(InvalidInputError, match=message):
        parse_gazetteer(json.dumps({"version": "t", "entries": entries}).encode())


def _doc(digest: str, path: str, cover: str = "", header: tuple[str, ...] = ()) -> DocumentInput:
    lines = _lines(cover) if cover else ()
    return DocumentInput(digest * 64, f"evd:{digest}", 3, (Placement(path),), header, lines)


def test_build_links_documents_with_evidence_scopes_and_statuses() -> None:
    gazetteer = parse_gazetteer(PACKAGED.read_bytes())
    documents = [
        _doc("a", f"{BLEIDA}/Rapport de pilotage du Far West de bleida- 05-2016-.pdf",
             cover=COVER, header=("CR/AF/BL/RP/10/17",)),
        _doc("b", f"{BLEIDA}/Rapport de pilotage du Far West de bleida- 05-2016.pdf"),
        _doc("c", f"{EIE}/Chapter 4_E Project Description_08122021_R1.pdf"),
        _doc("d", f"{EIE}/Chapter 4_E Project Description_14122021_R2.pdf"),
        _doc("e", "LIMS/Offre technique LIMS.pdf"),
    ]
    run, report = build_run(documents, gazetteer, today=TODAY)
    assert run.validate() == []
    assert report["documents_in_graph"] == 4
    assert report["totals"]["excluded_documents"] == 1
    entities = {entity.iri: entity for entity in run.entities}
    assert document_iri("e" * 64) not in entities
    first = entities[document_iri("a" * 64)]
    assert first.attributes["title"] == "Caractérisation des échantillons poudres SONASID"
    assert (first.attributes["date"], first.attributes["genre"]) == ("2016-05", "report")
    assert "ALANSARI" not in json.dumps(first.attributes, ensure_ascii=False)
    edges = {(a.subject, a.predicate, a.object, a.status) for a in run.assertions}
    a, b, c, d = (document_iri(x * 64) for x in "abcd")
    assert (a, "hasCode", "urn:crv:code:cr-af-bl-rp-10-17", "observed") in edges
    assert (a, "aboutSite", "urn:crv:site:bleida", "candidate") in edges  # folder and code BL
    assert (a, "issuedBy", "urn:crv:org:reminex", "candidate") in edges
    assert (a, "attributedTo", "urn:crv:org:sonasid", "candidate") in edges
    assert (a, "duplicateOf", b, "candidate") in edges  # the shorter path is canonical
    assert (d, "supersedes", c, "observed") in edges
    assert any(edge[0] == d and edge[1] == "partOf" for edge in edges)
    evidence = {item.iri: item for item in run.evidence}
    assert {evidence[iri].scope_key for iri in first.evidence} == {"crv:research"}
    site_scopes = {evidence[iri].scope_key for iri in entities["urn:crv:site:bleida"].evidence}
    assert {"crv:research", "crv:gazetteer"} <= site_scopes
    assert entities["urn:crv:org:somifer"].status == "candidate"  # operator of Bleida
    rerun, _ = build_run(list(reversed(documents)), gazetteer, today=TODAY)
    assert rerun.run_id == run.run_id


def test_projection_is_deterministic_and_scope_carrying() -> None:
    gazetteer = parse_gazetteer(PACKAGED.read_bytes())
    run, _ = build_run(
        [_doc("a", f"{GED}/Documents Activité/SMI/Rapport sur Imiter- 01-2020-.pdf")],
        gazetteer,
        today=TODAY,
    )
    scope = {item.iri: item.scope_key for item in run.evidence}
    entities = [
        ProjectedEntity(e.iri, e.kind, e.label, e.status, dict(e.attributes),
                        tuple(sorted({scope[i] for i in e.evidence})))
        for e in run.entities
    ]
    assertions = [
        ProjectedAssertion(a.iri, a.subject, a.predicate, a.object, a.method, a.status,
                           tuple(sorted({scope[i] for i in a.evidence})))
        for a in run.assertions
    ]
    projection = build_projection(entities, assertions, statuses=DEFAULT_RELEASE_STATUSES)
    again = build_projection(
        reversed(entities), reversed(assertions), statuses=DEFAULT_RELEASE_STATUSES
    )
    assert projection.manifest_sha256 == again.manifest_sha256
    assert projection.graph_name.startswith("crv_research_")
    labels = {edge.label for edge in projection.edges}
    assert {"IN_COLLECTION", "FILED_UNDER", "CONCERNS", "ABOUT_SITE"} <= labels
    observed_only = build_projection(entities, assertions, statuses=("observed",))
    assert "CONCERNS" not in {edge.label for edge in observed_only.edges}
    assert all(vertex.scope_keys for vertex in projection.vertices)
    assert fold("Ouanssimi, Tizert!") == "ouanssimi tizert"
