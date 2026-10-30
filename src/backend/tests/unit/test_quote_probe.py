import json
from pathlib import Path

import pytest

from crv_agent.domain.evidence import Claim, Evidence
from crv_agent.evaluation import quote_probe as probe
from crv_agent.orchestration.extraction import batched, extraction_messages, passage
from crv_agent.services.evidence_pack import expose

TEXT = (
    "Le taux de récupération du cuivre atteint 92,5 % après 48 heures.\n"
    "La consommation d'énergie est de 14 kWh/t pour le broyage pri-\nmaire du minerai."
)


def evidence(content: str = TEXT, eid: str = "S1", span: tuple[int, int] | None = None) -> Evidence:
    item = Evidence(
        id=eid, release="r1", chunk_id="c1", content_hash="a" * 64, title="Synthetic report",
        content=content, source_uri="sources/a.pdf", pages=[3], locators=["p3"],
        original_sources=[], generated=False, document_class="report",
    )
    return expose(item, span) if span else item


def claim(quote: str, value: str | None = None, ids: list[str] | None = None) -> Claim:
    return Claim(
        text="Synthetic claim", evidence_ids=ids or ["S1"], quote=quote, subject="ore",
        quantity="recovery", value=value, unit="%", date=None, conditions=None, scope=None,
    )


@pytest.mark.parametrize(("quote", "expected"), [
    ("", "empty"),
    ("taux de récupération du cuivre atteint 92,5 %", "located"),
    ("TAUX  de récupération", "located"),
    ("de 14 kWh/t pour le broyage pri-\\nmaire", "json_escape"),
    ("le broyage primaire du minerai", "line_hyphen"),
    ("Le taux de recuperation du cuivre", "accents"),
    ("Le taux de récupération du cuivre; la consommation d'énergie est de 14 kWh/t", "spliced"),
    ("Le taux de récupération du cuivre atteint 95 % après 48 heures", "near_copy"),
    ("Le taux de récupération du cuivre atteint un niveau record selon les essais de "
     "laboratoire", "partial_copy"),
    ("récupération heures cuivre taux atteint", "paraphrase"),
    ("The sulfide flotation circuit was redesigned", "not_in_passages"),
])
def test_classify_quote_names_the_cheapest_explanation(quote: str, expected: str) -> None:
    assert probe.classify_quote(quote, [TEXT]) == expected


def test_normalization_helpers() -> None:
    from crv_agent.services.anchoring import unescape

    assert unescape('a\\nb \\"c\\" \\u00e9') == 'a\nb "c" é'
    assert probe.join_line_hyphens("pri-\nmaire and pri- maire") == "primaire and primaire"
    assert probe.strip_accents("récupération") == "recuperation"
    assert probe.containment("a b c d", "x a b c d y") == 1.0
    assert probe.containment("a b", "b a", size=1) == 1.0
    assert probe.containment("", "anything") == 0.0
    assert "92,5" in probe.nearest_window("cuivre atteint 92,5", [TEXT, "unrelated"])


def test_judge_claim_uses_production_anchoring_and_validation() -> None:
    shown = [evidence()]
    exact = probe.judge_claim(claim("atteint 92,5 %", "92.5"), shown)
    assert (exact.raw_exact, exact.accepted, exact.category) == (True, True, "accepted")

    cased = probe.judge_claim(claim("ATTEINT 92,5 %", "92.5"), shown)
    assert (cased.raw_exact, cased.anchored, cased.accepted) == (False, True, True)
    assert cased.claim.quote == "atteint 92,5 %"

    wrong_value = probe.judge_claim(claim("atteint 92,5 %", "90"), shown)
    assert wrong_value.category == "value_not_in_quote"

    paraphrase = probe.judge_claim(claim("The flotation circuit was redesigned"), shown)
    assert (paraphrase.accepted, paraphrase.category) == (False, "not_in_passages")


def test_score_aggregates_per_arm_and_lists_misses() -> None:
    frozen = [{"question_id": "q1", "evidence": [evidence().model_dump(mode="json")]}]
    good = claim("atteint 92,5 %", "92.5").model_dump(mode="json")
    bad = claim("le broyage primaire du minerai").model_dump(mode="json")
    calls = [
        {"question_id": "q1", "label": "m", "batches": 1, "batch": 0, "seconds": 2.0,
         "output_tokens": 100, "error": None, "claims": [good, bad]},
        {"question_id": "q1", "label": "n", "batches": 2, "batch": 0, "seconds": 1.0,
         "error": "RateLimitError", "claims": []},
        {"question_id": "other", "label": "m", "batches": 1, "batch": 0, "claims": [good]},
    ]
    report, misses = probe.score(frozen, calls, max_claims=8)
    first, second = report["arms"]
    assert (first["label"], first["claims"], first["accepted_rate"]) == ("m", 2, 0.5)
    assert first["categories"] == {"accepted": 1, "line_hyphen": 1}
    assert first["questions_with_accepted_claim"] == 1
    assert (second["call_errors"], second["claims"], second["accepted_rate"]) == (1, 0, None)
    [miss] = misses
    assert miss["category"] == "line_hyphen" and "pri-" in miss["nearest_source"]
    assert "line_hyphen 1" in probe.summary_markdown(report)
    capped, _ = probe.score(frozen, calls[:1], max_claims=1)
    assert capped["arms"][0]["claims"] == 1


def test_frozen_evidence_reproduces_the_production_passages() -> None:
    hits = [(evidence(), (0, len(TEXT))), (evidence(eid="S2", span=None), (10, 40))]
    exposed = [expose(hit, span) for hit, span in hits]
    record = {"evidence": [item.model_dump(mode="json") for item in exposed]}
    rebuilt = probe.shown_passages(probe.frozen_evidence(record))
    assert rebuilt == [passage(hit, span) for hit, span in hits]
    assert rebuilt[1]["truncated"] is True and rebuilt[1]["text"] == TEXT[10:40]
    assert [len(group) for group in batched(rebuilt, 2)] == [1, 1]
    payload = json.loads(str(extraction_messages("Q?", rebuilt, 0, 8, [])[1].content))
    assert payload["passages"] == rebuilt and payload["max_claims"] == 8


def test_selection_is_stratified_and_skips_abstentions() -> None:
    from crv_agent.evaluation.dataset import GoldenQuestion

    def question(qid: str, category: str, abstain: bool = False) -> GoldenQuestion:
        return GoldenQuestion(
            id=qid, question="?", category=category, split="dev", key_facts=[],
            must_abstain=abstain,
        )

    pool = [question("a2", "A"), question("a1", "A"), question("a3", "A"),
            question("b1", "B", abstain=True), question("b2", "B")]
    assert [q.id for q in probe.select_questions(pool, 2, [], False)] == ["a1", "a2", "b2"]
    assert len(probe.select_questions(pool, 2, [], True)) == 4
    assert [q.id for q in probe.select_questions(pool, 2, ["a3"], False)] == ["a3"]
    assert probe.parse_model("m@none") == ("m", "none")
    assert probe.parse_model("org/m") == ("org/m", None)


def test_plan_summary_counts_research_questions_routed_away_from_the_worker() -> None:
    def record(qid: str, category: str, intent: str | None, label: str = "m") -> dict[str, object]:
        return {"question_id": qid, "category": category, "label": label, "intent": intent,
                "facets": ["f"] if intent else [], "seconds": 1.0,
                "error": None if intent else "Timeout"}

    summary = probe.plan_summary([
        record("q1", "A", "lookup"), record("q2", "A", "catalogue"),
        record("q3", "B", "clarify"), record("q4", "E", "catalogue"),
        record("q5", "F", "out_of_scope"), record("q6", "A", None),
    ])
    [model] = summary["models"]
    assert (model["questions"], model["errors"], model["research_questions"]) == (6, 1, 3)
    assert (model["research_routed_away"], model["research_as_catalogue"]) == (2, 1)
    assert (model["catalogue_questions"], model["catalogue_recognised"]) == (1, 1)
    assert model["intents"]["catalogue"] == 2


def test_score_cli_writes_reports(tmp_path: Path) -> None:
    frozen = tmp_path / "passages.jsonl"
    runs = tmp_path / "runs.jsonl"
    frozen.write_text(json.dumps(
        {"question_id": "q1", "evidence": [evidence().model_dump(mode="json")]},
    ) + "\n", encoding="utf-8")
    runs.write_text(json.dumps({
        "question_id": "q1", "label": "m", "batches": 1, "batch": 0, "seconds": 1.0,
        "error": None, "claims": [claim("atteint 92,5 %", "92.5").model_dump(mode="json")],
    }) + "\n", encoding="utf-8")
    out = tmp_path / "score"
    assert probe.main([
        "score", "--frozen", str(frozen), "--runs", str(runs), "--out-dir", str(out),
    ]) == 0
    report = json.loads((out / "score.json").read_text(encoding="utf-8"))
    assert report["arms"][0]["accepted_rate"] == 1.0
    assert (out / "summary.md").exists() and (out / "misses.jsonl").exists()
    with pytest.raises(SystemExit):
        probe.main(["run", "--frozen", str(frozen), "--model", "m", "--out", str(runs)])
