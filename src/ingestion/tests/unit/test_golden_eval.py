"""Golden-set evaluation: categories, facts, anchors, matching and reciprocal-rank fusion."""

from __future__ import annotations

from ingest_pipeline.tools.golden.__main__ import anchor_scores, summarize
from ingest_pipeline.tools.golden.dataset import (
    AnchorText,
    anchor_hits,
    anchors_of,
    build_questions,
    category_of,
    split_facts,
)
from ingest_pipeline.tools.golden.retrieval import Hit, _fuse

ROWS = [(f"Question {n}", "Réponse ; détail") for n in range(1, 77)]


def _hit(chunk: str, text: str) -> Hit:
    return Hit(chunk, "a" * 64, "Titre", "Dossier Cu/x.pdf", text, (1,), None)


def test_categories_follow_the_documented_ranges() -> None:
    assert [category_of(n) for n in (1, 31, 44, 45, 52, 60, 64, 76)] == list("ABCDEFGA")
    questions = build_questions(ROWS)
    assert len(questions) == 76
    assert {q.id for q in questions if q.must_abstain} == {"Q60", "Q61", "Q62", "Q63"}
    assert sum(q.split == "heldout" for q in questions) == sum(
        1 for c in "ABCDEFG" for i in range(sum(q.category == c for q in questions)) if i % 3 == 2
    )


def test_facts_and_anchors_keep_numbers_codes_and_names() -> None:
    expected = ("500 kg d'échantillon, délai 1 mois, 160 000 DH HT ; (offre CR-AR-PE-02-15 : "
                "200 kg) ; Na2S2O5 (300 g/t, pH 11) + AXK : 22,5 % Cu ; plan Box-Behnken")
    facts = split_facts(expected)
    assert len(facts) == 4
    anchors = [anchors_of(fact, common=frozenset({"plan"})) for fact in facts]
    assert anchors[0] == ("#500", "#160000")
    assert anchors[1] == ("#200", "cr ar pe 02 15")  # code digits are not quantities
    assert {"#300", "#11", "#22.5", "na2s2o5", "axk"} <= set(anchors[2])
    assert anchors[3] == ("box behnken",)


def test_anchor_matching_tolerates_number_and_text_formatting() -> None:
    text = AnchorText("Campagne 1 : 7413 t ; teneur 22.5% Cu ; Na₂S₂O₅ ; rapport CR/AR/PE/02/15")
    assert anchor_hits(["#7413", "#22.5", "na2s2o5", "cr ar pe 02 15", "#741"], [text]) == {
        "#7413", "#22.5", "na2s2o5", "cr ar pe 02 15"
    }
    assert not AnchorText("pH 11,5").has("#11")


def test_anchor_scores_and_summary() -> None:
    question = build_questions([("Q?", "7 413 t ; 25,4 % Cu ; sans ancre"), *ROWS[1:]])[0]
    scores = anchor_scores(question, [_hit("c1", "7 413 t traitées"), _hit("c2", "autre")], 10)
    assert scores["anchor_recall"] == 0.5
    assert scores["fact_coverage"] == 0.5
    assert scores["missing"] == ["#25.4"]
    result = {"id": "Q1", "category": "A", "split": "dev", "modes": {"bm25": {
        "seconds": 0.2, "anchors": {"10": scores}, "judge": {"recall": 1.0}}}}
    summary = summarize([result], ["bm25"], [10], 10)
    assert summary["bm25"]["categories"]["A"]["anchor_recall@10"] == 0.5
    assert summary["bm25"]["categories"]["ALL"]["judge_recall@10"] == 1.0


def test_reciprocal_rank_fusion_rewards_agreement() -> None:
    steered = [_hit("a", ""), _hit("b", ""), _hit("c", "")]
    base = [_hit("c", ""), _hit("d", ""), _hit("a", "")]
    assert [hit.chunk_id for hit in _fuse([steered, base], 3)] == ["a", "c", "b"]
