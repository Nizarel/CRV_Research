import json
import re

from langgraph.runtime import Runtime

from crv_agent.domain.answers import Answer, AnswerDraft, Conflict, Verdict
from crv_agent.domain.evidence import Calculation, Claim
from crv_agent.domain.execution import BudgetExceeded
from crv_agent.orchestration.context import ResearchContext
from crv_agent.orchestration.state import ResearchState
from crv_agent.services.verification import ungrounded_numbers, verify_draft


async def answer(
    state: ResearchState, runtime: Runtime[ResearchContext],
) -> dict[str, object]:
    context, plan = runtime.context, state["plan"]
    status = "answered"
    reasons: list[str] = []
    evidence = state["evidence"]
    claims = state["claims"]
    conflicts: list[Conflict] = []
    if plan.intent == "clarify":
        status, text = "needs_clarification", plan.clarification or (
            "Veuillez préciser le site, le document ou l'essai."
            if plan.language == "fr" else "Please specify the site, document or study."
        )
    elif plan.intent == "out_of_scope":
        status, text = "out_of_scope", (
            "Cette information ne peut pas être établie à partir du corpus documentaire disponible."
            if plan.language == "fr" else
            "This information cannot be established from the available document corpus."
        )
    elif plan.intent == "procedure":
        status, text = "out_of_scope", (
            "Cette demande dépasse ce backend de recherche documentaire. "
            "Les protocoles dangereux ne sont jamais synthétisés."
            if plan.language == "fr" else
            "This request is outside this research backend. "
            "Hazardous protocols are never synthesized."
        )
    elif not claims:
        status, text = "insufficient_evidence", (
            "Les éléments retrouvés ne permettent pas de répondre de façon étayée."
            if plan.language == "fr" else
            "The retrieved evidence is insufficient to support an answer."
        )
    else:
        details = json.dumps({
            "coverage": state["coverage"].model_dump(),
            "conflicts": [c.model_dump() for c in state["conflicts"]],
            "calculations": [c.model_dump(mode="json") for c in state["calculations"]],
            "premises_to_verify": plan.premises,
        }, ensure_ascii=False)
        draft: AnswerDraft | None = None
        verdict: Verdict | None = None
        pruned = listed = False
        try:
            for round_number in range(2):
                composed = await context.models.compose(
                    state["question"], claims, details + "\n" + "\n".join(reasons),
                    context.budget,
                )
                checked = verify_draft(
                    composed, evidence, context.search_release, state["calculations"],
                )
                used = {cid for claim in composed.claims for cid in claim.evidence_ids}
                judged = await context.models.verify(
                    composed, [item for item in evidence if item.id in used], details,
                    context.budget,
                )
                # Draft, verdict and reasons only change together, so a budget stop below
                # never pairs a new draft with the previous round's verdict.
                draft, verdict = composed, judged
                reasons = checked + (judged.reasons if not judged.supported else [])
                if not reasons:
                    break
                if context.repair_mode == "prune" and round_number == 0:
                    candidate = prune_ungrounded(draft, verdict, state["calculations"])
                    if candidate is not None and not verify_draft(
                        candidate, evidence, context.search_release, state["calculations"],
                    ):
                        draft, reasons, pruned = candidate, [], True
                        break
                # Converge on the single repair: drop exactly the claims the verifier rejected,
                # rather than asking the composer to rewrite a mis-bound claim it cannot edit.
                rejected = [
                    draft.claims[n] for n in verdict.unsupported_claims
                    if 0 <= n < len(draft.claims)
                ]
                claims = [claim for claim in claims if claim not in rejected]
                if not claims:
                    break
        except BudgetExceeded:
            if draft is None:
                reasons = ["The turn budget ran out before an answer could be verified."]
        if reasons and draft is not None and verdict is not None:
            candidate = prune_ungrounded(draft, verdict, state["calculations"])
            if candidate is not None and not verify_draft(
                candidate, evidence, context.search_release, state["calculations"],
            ):
                draft, reasons, pruned = candidate, [], True
            elif (
                candidate := findings(draft, verdict, state["calculations"], plan.language)
            ) is not None and not verify_draft(
                candidate, evidence, context.search_release, state["calculations"],
            ):
                draft, reasons, listed = candidate, [], True
        if reasons or draft is None:
            status, text, claims = "insufficient_evidence", (
                "La réponse n'a pas passé la vérification des sources."
                if plan.language == "fr" else "The answer did not pass source verification."
            ), []
        else:
            text, claims = draft.content, draft.claims
            conflicts = answer_conflicts(draft, state["claims"], state["conflicts"])
            # Removed claims are listed in reasons; only unanswered or cut-short work is partial.
            status = "insufficient_evidence" if draft.insufficient else (
                "partial" if draft.unanswered or state["coverage"].incomplete or listed
                else "answered"
            )
            if pruned:
                reasons = ["Statements the verifier found unsupported were removed."]
            if listed:
                reasons = ["The synthesis failed verification; only verified findings are listed."]
            label = "Non couvert : " if plan.language == "fr" else "Not answered: "
            reasons += [label + part for part in draft.unanswered]
    selected = {cid for claim in claims for cid in claim.evidence_ids}
    result = Answer.model_validate({
        "status": status, "content": text,
        "claims": claims if selected else [],
        "citations": [item for item in evidence if item.id in selected],
        "conflicts": conflicts, "calculations": state["calculations"],
        "coverage": state["coverage"], "reasons": reasons,
        "search_release": context.search_release, "kg_release": context.kg_release,
        "trace_id": context.trace_id, "usage": context.budget.usage(),
        "role_usage": context.budget.role_usage(),
        "retrieval_mode": context.retrieval_mode,
    })
    return {"answer": result}


_MARKER = re.compile(r"\[([^\[\]]+)\]")
_TRAILING_MARKERS = r"(?:[ \t]*\[[^\[\]\n]+\])*"
_CITATIONS_ONLY = re.compile(r"[\s.,;:-]*(?:\[[^\[\]\n]+\][\s.,;:-]*)+")
_EMPTY_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s*")
_HEADING = re.compile(r"(#{1,6})\s+\S.*|\*\*[^*\n]+\*\*\s*[:.]?")


def heading_level(line: str) -> int | None:
    """Markdown heading depth; a bold-only line is the deepest level (7)."""
    match = _HEADING.fullmatch(line.strip())
    if match is None:
        return None
    return len(match.group(1)) if match.group(1) else 7


def tidy(content: str) -> str:
    """Drop citation-only lines, empty list items and headings that pruning left bodiless."""
    lines = [
        line for line in content.split("\n")
        if not _CITATIONS_ONLY.fullmatch(line) and not _EMPTY_ITEM.fullmatch(line)
    ]
    while True:
        kept = []
        for n, line in enumerate(lines):
            level = heading_level(line)
            following = next((later for later in lines[n + 1:] if later.strip()), None)
            if level is not None and (
                following is None
                or ((deeper := heading_level(following)) is not None and deeper <= level)
            ):
                continue
            kept.append(line)
        if kept == lines:
            break
        lines = kept
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", "\n".join(lines))).strip()


def prune_ungrounded(
    draft: AnswerDraft, verdict: Verdict, calculations: list[Calculation],
) -> AnswerDraft | None:
    """Prune verifier-rejected content plus sentences carrying numbers no claim grounds."""
    loose = ungrounded_numbers(draft.content, draft.claims, calculations)
    return prune(draft, verdict.model_copy(update={"unsupported_sentences": [
        *verdict.unsupported_sentences,
        *(s for s in sentences(draft.content) if any(n in s for n in loose)),
    ]}))


def prune(draft: AnswerDraft, verdict: Verdict) -> AnswerDraft | None:
    """Remove verbatim unsupported sentences and rejected claims; None if nothing remains.

    Only deletes text, so it cannot introduce unsupported content; the caller re-runs the
    deterministic citation contract on the result.
    """
    if not verdict.unsupported_sentences and not verdict.unsupported_claims:
        return None
    content = draft.content
    for sentence in verdict.unsupported_sentences:
        if sentence.strip() and sentence.strip() in content:
            content = re.sub(re.escape(sentence.strip()) + _TRAILING_MARKERS, "", content)
    content = tidy(content)
    rejected = {n for n in verdict.unsupported_claims if 0 <= n < len(draft.claims)}
    while True:
        markers = set(re.findall(r"\[([^\[\]]+)\]", content))
        claims = [
            claim for n, claim in enumerate(draft.claims)
            if n not in rejected and set(claim.evidence_ids) <= markers
        ]
        bound = {cid for claim in claims for cid in claim.evidence_ids}
        orphans = [s for s in sentences(content) if set(_MARKER.findall(s)) - bound]
        if not orphans:
            break
        for sentence in orphans:
            content = content.replace(sentence, "")
        content = tidy(content)
    if not content or not claims or content == draft.content.strip():
        return None
    kept = {id(claim): n for n, claim in enumerate(claims)}
    conflicts = [
        conflict.model_copy(update={"claim_indices": indices})
        for conflict in draft.conflicts
        if len(indices := sorted(
            kept[id(draft.claims[i])] for i in conflict.claim_indices
            if id(draft.claims[i]) in kept
        )) >= 2
    ]
    return draft.model_copy(update={"content": content, "claims": claims, "conflicts": conflicts})


def findings(
    draft: AnswerDraft, verdict: Verdict, calculations: list[Calculation], language: str,
) -> AnswerDraft | None:
    """Unrejected, self-grounded claims listed with citations; None for a blanket rejection."""
    if not verdict.unsupported_claims and not verdict.unsupported_sentences:
        return None
    rejected = set(verdict.unsupported_claims)
    lines: list[str] = []
    claims: list[Claim] = []
    for n, claim in enumerate(draft.claims):
        line = f"- {claim.text} " + " ".join(f"[{cid}]" for cid in claim.evidence_ids)
        if n in rejected or _MARKER.search(claim.text) or ungrounded_numbers(
            line, [claim], calculations,
        ):
            continue
        lines.append(line)
        claims.append(claim)
    if not claims:
        return None
    intro = (
        "La synthèse n'a pas passé la vérification des sources ; seuls les constats "
        "vérifiés sont listés." if language == "fr" else
        "The synthesis did not pass source verification; only verified findings are listed."
    )
    return AnswerDraft(
        content=intro + "\n\n" + "\n".join(lines), claims=claims,
        insufficient=draft.insufficient,
    )


def answer_conflicts(
    draft: AnswerDraft, ledger: list[Claim], detected: list[Conflict],
) -> list[Conflict]:
    """Composer-identified conflicts plus deterministic ones, indexed into the answer claims."""
    position = {claim.model_dump_json(): n for n, claim in enumerate(draft.claims)}
    result = list(draft.conflicts)
    for conflict in detected:
        indices = sorted({
            position[key] for i in conflict.claim_indices
            if 0 <= i < len(ledger) and (key := ledger[i].model_dump_json()) in position
        })
        if len(indices) >= 2 and all(set(indices) != set(c.claim_indices) for c in result):
            result.append(conflict.model_copy(update={"claim_indices": indices}))
    return result


def sentences(content: str) -> list[str]:
    """Sentence or list-item units of the answer, for verbatim pruning."""
    return [
        part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", content) if part.strip()
    ]
