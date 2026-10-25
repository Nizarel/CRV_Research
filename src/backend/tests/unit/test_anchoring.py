"""Claim re-anchoring and the numeric/qualifier check (validator false rejections, AUD audit)."""

from crv_agent.domain.evidence import Claim, Evidence
from crv_agent.services.anchoring import anchor_claim, locate
from crv_agent.services.verification import claim_errors, stated, value_stated

PAGE = Evidence(
    id="S1", release="r1", chunk_id="c1", content_hash="a" * 64, title="Pilot report",
    content="Résultats :\n\n• Récupération en cuivre aux alentours de 85 % ;\n\n"
    "• Teneur du concentré : 21,5\u00a0% Cu ;\n\n• Tonnage traité : 13 360 t.",
    source_uri="raw/report.pdf", pages=[4], locators=[], original_sources=[], generated=False,
    document_class="report",
)
OTHER = PAGE.model_copy(update={"id": "S2", "content": "Unrelated annex text."})


def claim(quote: str, value: str | None, qualifier: str | None = None,
          ids: list[str] | None = None, calculation: int | None = None) -> Claim:
    return Claim(
        text="c", evidence_ids=ids or ["S1"], quote=quote, subject="Tizert", quantity="recovery",
        value=value, unit="%", date=None, conditions=None, scope=None,
        calculation_index=calculation, qualifier=qualifier,  # type: ignore[arg-type]
    )


def test_qualifiers_printed_as_words_and_grouped_thousands_are_stated_values() -> None:
    assert value_stated("aux alentours de 85 %", "85", "~")
    assert value_stated("environ 12% de la capacité", "12", "~")
    assert value_stated("au moins 99,995% en cuivre", "99.995", ">=")
    assert value_stated("plus de 25 % Cu", "25", ">")
    assert value_stated("Tonnage traité : 13 360 t.", "13360", None)
    assert value_stated("pour un lot de 10'000 t.", "10000", None)
    assert value_stated("≈ 43 %", "43", "~")
    assert not value_stated("au moins 99,995%", "99.995", "<")
    assert value_stated("Récupération 85 %", "85", "~")
    assert not value_stated("Récupération ≈ 85 %", "85", None)
    assert not value_stated("Récupération 85 %", "86", None)


def test_bounds_phrased_as_report_wording_are_stated_values() -> None:
    assert value_stated("La récupération ne dépasse pas les 22%.", "22", "<=")
    assert not value_stated("La récupération ne dépasse pas les 22%.", "22", ">")
    assert value_stated("la teneur en Zinc qui n'a pas dépassé les 2%", "2", "<=")
    assert value_stated("occupant jusqu'à 50 % du volume", "50", "<=")
    assert value_stated("se maintenir au dessus de 23% Cu", "23", ">")
    assert value_stated("la teneur en cuivre dépasse les 40% après", "40", ">")
    assert not value_stated("seuls 4% des concentrés", "4", "<")


def test_small_counts_written_as_words_need_their_unit() -> None:
    campaigns = claim("Trois campagnes de pilotage industriel ont été réalisées", "3").model_copy(
        update={"unit": "campagnes"},
    )
    assert stated(campaigns, campaigns.quote)
    assert not stated(campaigns.model_copy(update={"qualifier": "~"}), campaigns.quote)
    relavages = claim("huit relavages au lieu de cinq", "5")
    assert not stated(relavages, relavages.quote)


def test_quotes_differing_in_case_or_table_cells_anchor_on_the_exact_source_span() -> None:
    assert locate("La première attaque s'effectue", "Ainsi, la première attaque s'effectue à") == (
        "la première attaque s'effectue"
    )
    row = "| Rdt reconstitué | Stérile | 81,8 | % | | | Solution | 77,6 | % |"
    assert locate("Rdt reconstitué | Stérile | 81,8 | % | | Solution | 77,6 | %", row) == (
        "Rdt reconstitué | Stérile | 81,8 | % | | | Solution | 77,6 | %"
    )
    assert locate("varie entre 69.3 et 96.1% ; v Le rendement", "96.1% ; :selected: v Le") is None
    assert locate("96.1% ; v Le rendement", "entre 69.3 et 96.1% ; :selected: v Le rendement") == (
        "96.1% ; :selected: v Le rendement"
    )


def test_an_elided_quote_widens_to_one_close_exact_span() -> None:
    page = PAGE.model_copy(update={"content": (
        "Les essais confirment la dépression de la pyrite par NaCN à pH 10.5. Le cuivre est "
        "flotté par AXK. Les meilleurs résultats sont ceux de l'essai 4." + " filler" * 200
        + " Conclusion lointaine du rapport."
    )})
    elided = claim(
        "la dépression de la pyrite par NaCN [...] Les meilleurs résultats sont ceux", None,
    )
    widened = anchor_claim(elided, [page])
    assert widened.quote == (
        "la dépression de la pyrite par NaCN à pH 10.5. Le cuivre est flotté par AXK. Les "
        "meilleurs résultats sont ceux"
    )
    assert claim_errors(widened, {"S1": page}) == []
    far = claim("la dépression de la pyrite par NaCN ... Conclusion lointaine du rapport", None)
    assert anchor_claim(far, [page]).quote == far.quote


def test_anchoring_repairs_spacing_ids_stitching_and_spurious_calculations() -> None:
    assert locate("Teneur du concentré : 21,5 % Cu", PAGE.content) == (
        "Teneur du concentré : 21,5\u00a0% Cu"
    )
    spaced = anchor_claim(claim("Teneur du concentré :  21,5 % Cu", "21.5"), [PAGE])
    assert spaced.quote == "Teneur du concentré : 21,5\u00a0% Cu"
    rebound = anchor_claim(claim("Tonnage traité : 13 360 t.", "13360", ids=["S9"]), [OTHER, PAGE])
    assert rebound.evidence_ids == ["S1"]
    adjacent = anchor_claim(claim(
        "• Récupération en cuivre aux alentours de 85 % ;\n• Teneur du concentré : 21,5 % Cu ;",
        "85", "~",
    ), [PAGE])
    assert adjacent.quote.endswith("21,5\u00a0% Cu ;")  # blank lines only: one shown span
    stitched = anchor_claim(claim(
        "• Récupération en cuivre aux alentours de 85 % ;\n• Tonnage traité : 13 360 t.",
        "85", "~",
    ), [PAGE])
    assert stitched.quote == "• Récupération en cuivre aux alentours de 85 % ;"
    derived = anchor_claim(claim("Tonnage traité : 13 360 t.", "13360", calculation=0), [PAGE])
    assert derived.calculation_index is None
    for repaired in (spaced, rebound, adjacent, stitched, derived):
        assert claim_errors(repaired, {"S1": PAGE}) == []


def test_a_date_whose_year_the_cited_passage_does_not_print_is_cleared() -> None:
    dated = PAGE.model_copy(update={"content": PAGE.content + "\nRévision du 25/02/2016."})
    from_filename = claim("Tonnage traité : 13 360 t.", "13360").model_copy(
        update={"date": "02-2016"},
    )
    assert anchor_claim(from_filename, [PAGE]).date is None
    assert anchor_claim(from_filename, [dated]).date == "02-2016"
    undated = from_filename.model_copy(update={"date": "février"})
    assert anchor_claim(undated, [PAGE]).date == "février"


def test_anchoring_never_accepts_text_that_was_not_shown() -> None:
    invented = claim("Récupération en cuivre de 92 %", "92")
    assert anchor_claim(invented, [PAGE]) == invented
    assert claim_errors(invented, {"S1": PAGE})
    wrong_value = claim("Récupération en cuivre aux alentours de 85 %", "92", "~")
    assert claim_errors(anchor_claim(wrong_value, [PAGE]), {"S1": PAGE})
