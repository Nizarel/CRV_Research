import json

from langgraph.runtime import Runtime

from crv_agent.domain.answers import Answer, AnswerDraft
from crv_agent.orchestration.context import ResearchContext
from crv_agent.orchestration.state import ResearchState
from crv_agent.services.verification import verify_draft


async def answer(
    state: ResearchState, runtime: Runtime[ResearchContext],
) -> dict[str, object]:
    context, plan = runtime.context, state["plan"]
    status = "answered"
    reasons: list[str] = []
    evidence = state["evidence"]
    claims = state["claims"]
    if plan.intent == "clarify":
        status, text = "needs_clarification", plan.clarification or (
            "Veuillez préciser le site, le document ou l'essai."
            if plan.language == "fr" else "Please specify the site, document or study."
        )
    elif plan.intent in {"out_of_scope", "procedure"}:
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
        for _ in range(2):
            draft = await context.models.compose(
                state["question"], claims, details + "\n" + "\n".join(reasons), context.budget,
            )
            reasons = verify_draft(
                draft, evidence, context.search_release, state["calculations"],
            )
            used = {cid for claim in draft.claims for cid in claim.evidence_ids}
            verdict = await context.models.verify(
                draft, [item for item in evidence if item.id in used], details, context.budget,
            )
            reasons.extend(verdict.reasons if not verdict.supported else [])
            if not reasons:
                break
        if reasons or draft is None:
            status, text, claims = "insufficient_evidence", (
                "La réponse n'a pas passé la vérification des sources."
                if plan.language == "fr" else "The answer did not pass source verification."
            ), []
        else:
            text, claims = draft.content, draft.claims
            status = "insufficient_evidence" if draft.insufficient else (
                "partial" if state["coverage"].gaps else "answered"
            )
    selected = {cid for claim in claims for cid in claim.evidence_ids}
    result = Answer.model_validate({
        "status": status, "content": text,
        "claims": claims if selected else [],
        "citations": [item for item in evidence if item.id in selected],
        "conflicts": state["conflicts"], "calculations": state["calculations"],
        "coverage": state["coverage"], "reasons": reasons,
        "search_release": context.search_release, "kg_release": context.kg_release,
        "trace_id": context.trace_id, "usage": context.budget.usage(),
        "retrieval_mode": context.retrieval_mode,
    })
    return {"answer": result}
