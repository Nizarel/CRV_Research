import asyncio
import json
import logging

from langgraph.runtime import Runtime

from crv_agent.domain.answers import Coverage
from crv_agent.domain.evidence import (
    Calculation,
    CalculationRequest,
    Claim,
    Evidence,
    TracedWorkerResult,
    WorkerResult,
)
from crv_agent.domain.execution import BudgetExceeded
from crv_agent.domain.planning import Document, Facet, QueryPlan
from crv_agent.orchestration.context import ResearchContext
from crv_agent.orchestration.state import ResearchState
from crv_agent.services.anchoring import anchor_claim
from crv_agent.services.calculation import calculate
from crv_agent.services.catalogue import enumerate_documents
from crv_agent.services.evidence_pack import merge_exposure
from crv_agent.services.kg_guidance import KG_INTENTS, kg_documents
from crv_agent.services.navigation import steering_entities
from crv_agent.services.policy import hazardous_instructions
from crv_agent.services.reconciliation import conflicts
from crv_agent.services.verification import claim_errors

logger = logging.getLogger(__name__)


async def understand(
    state: ResearchState, runtime: Runtime[ResearchContext],
) -> dict[str, object]:
    context = runtime.context
    if hazardous_instructions(state["question"]):
        plan = QueryPlan(
            intent="procedure", language="en"
            if state["question"].lower().startswith(("how ", "what ", "which ")) else "fr",
            facets=[], identifiers=[], premises=[],
            clarification=None,
        )
    else:
        plan = await context.models.plan(state["question"], state["history"], context.budget)
    return {
        "plan": plan, "claims": [], "evidence": [], "calculations": [], "conflicts": [],
        "coverage": Coverage(),
    }


async def research(
    state: ResearchState, runtime: Runtime[ResearchContext],
) -> dict[str, object]:
    context, plan = runtime.context, state["plan"]
    if plan.intent in {"clarify", "out_of_scope", "procedure"}:
        return {}
    entities = []
    if context.retrieval_mode == "kg":
        await context.budget.reserve(retrieval=1)
        entities = await context.budget.wait(
            context.knowledge.resolve(state["question"], context.kg_release),
        )
    gaps = [f"Candidate entity mapping: {e.name}" for e in entities if e.status == "candidate"]
    coverage = Coverage(gaps=gaps)
    hashes: list[str] = []
    docs: list[Document] = []
    if context.retrieval_mode == "search":
        gaps.append("Search-only baseline; no KG catalogue enumeration or navigation.")
    navigation = steering_entities(entities, plan.intent)
    if context.retrieval_mode == "kg" and (plan.intent == "catalogue" or navigation):
        try:
            docs, coverage = await enumerate_documents(
                context.knowledge, [e.iri for e in navigation], context.kg_release, context.budget,
            )
        except BudgetExceeded:
            return {"coverage": Coverage(gaps=["Catalogue enumeration exceeded the turn budget."])}
        hashes = list(dict.fromkeys(doc.content_hash for doc in docs if doc.content_hash))
        gaps.extend(coverage.gaps)
        if any(not doc.content_hash for doc in docs):
            gaps.append("Some KG documents have no Search-compatible content hash.")
    if plan.intent == "catalogue":
        items = []
        for doc in docs:
            await context.budget.reserve(retrieval=1)
            lineage = await context.budget.wait(
                context.knowledge.lineage(doc.iri, context.kg_release),
            )
            items.append({"document": doc.model_dump(mode="json"), "lineage": lineage})
            if len(items) == 12 and len(docs) > 12:
                gaps.append("Catalogue details limited to 12 documents; narrow the request.")
                coverage = coverage.model_copy(update={"incomplete": True})
                break
        # Catalogue observations retain an explicit filename/manifest basis, not scientific facts.
        evidence = [
            Evidence(
                id=f"catalogue:{i}", release=context.search_release, chunk_id="",
                content_hash=doc.content_hash or "", title=doc.title,
                content=json.dumps(items[i], ensure_ascii=False), source_uri=doc.source_paths[0]
                if doc.source_paths else doc.iri, pages=[],
                locators=[f"catalogue:{doc.iri}"], original_sources=doc.source_paths,
                generated=False, document_class="catalogue_metadata",
            ) for i, doc in enumerate(docs[:len(items)])
        ]
        catalogue_claims = [
            Claim(
                text=f"Catalogue metadata (filename/manifest basis): {item.title}",
                evidence_ids=[item.id], quote=item.content, subject=item.title,
                quantity=None, value=None, unit=None, date=None, conditions=None, scope=None,
            ) for item in evidence
        ]
        return {
            "evidence": evidence, "claims": catalogue_claims,
            "coverage": coverage.model_copy(update={"visited": len(items), "gaps": gaps}),
        }
    # (facet, documents, strict): a strict facet searches only its documents.
    runs = [(facet, hashes, False) for facet in plan.facets or [Facet(question=state["question"])]]
    if (
        context.kg_results and context.retrieval_mode == "kg" and navigation
        and plan.intent in KG_INTENTS
    ):
        try:
            await context.budget.reserve(retrieval=1)
            studies = await context.budget.wait(context.knowledge.studies(
                [e.iri for e in navigation], context.kg_release,
            ))
        except BudgetExceeded:
            studies = []
        if chosen := kg_documents(studies):
            # First, so a large plan cannot exhaust the turn budget before it searches.
            runs.insert(0, (Facet(question=state["question"]), chosen, True))
            gaps.append("KG-2 studies chose the documents of one search; cited text only.")
    gaps.append("Top-k evidence retrieval is not an exhaustive corpus review.")
    semaphore = asyncio.Semaphore(context.facet_concurrency)
    evidence_by_id: dict[str, Evidence] = {}
    claims: dict[str, Claim] = {}
    audit: list[dict[str, object]] = []

    async def run(facet: Facet, scope: list[str], strict: bool) -> None:
        accepted: list[Claim] = []
        status = "completed"
        result: WorkerResult | None = None
        async with semaphore:
            try:
                result, found = await context.worker.run(
                    facet.question + "\nOriginal question: " + state["question"]
                    + "\nPremises to verify: " + "; ".join(plan.premises),
                    context.search_release, scope, context.budget, strict=strict,
                )
                for item in found:
                    previous = evidence_by_id.get(item.id)
                    evidence_by_id[item.id] = (
                        item if previous is None else merge_exposure(previous, item)
                    )
                gaps.extend(result.gaps)
                for claim in (anchor_claim(raw, found) for raw in result.claims):
                    errors = claim_errors(claim, evidence_by_id)
                    if not errors:
                        claims[claim.model_dump_json()] = claim
                        accepted.append(claim)
                    else:
                        logger.warning("worker_claim_rejected errors=%s", "; ".join(errors))
                        gaps.append("A worker claim failed source/locator/value validation.")
            except BudgetExceeded:
                status = "budget_exhausted"
                gaps.append("A research facet exhausted the shared execution budget.")
            except Exception as error:
                status = "failed"
                failures.append(error)
                logger.warning("research_facet_failed error_type=%s", type(error).__name__)
                gaps.append(
                    f"A research facet failed ({type(error).__name__}); its evidence is missing.",
                )
        if context.diagnostics:
            audit.append(facet_audit(facet.question, status, result, evidence_by_id))
        if status != "completed":
            cut_short.append(facet.question)
        # Early findings, not a verified answer: every streamed claim's quote is located verbatim.
        cited = {cid for claim in accepted for cid in claim.evidence_ids}
        runtime.stream_writer({
            "event": "facet", "facet": facet.question, "status": status,
            "claims": [claim.model_dump(mode="json") for claim in accepted],
            "sources": [
                {
                    "id": item.id, "title": item.title,
                    "source": item.source_path or item.source_uri, "pages": item.pages,
                    "document_code": item.document_code, "source_role": item.source_role,
                } for item in sorted(evidence_by_id.values(), key=lambda e: e.id)
                if item.id in cited
            ],
        })
    failures: list[Exception] = []
    cut_short: list[str] = []
    async with asyncio.TaskGroup() as group:
        for facet, scope, strict in runs:
            group.create_task(run(facet, scope, strict))
    if failures and len(failures) == len(runs):
        # Every facet failed: a provider/tool failure, never an absence-of-evidence answer.
        raise failures[0]
    visited = len({e.content_hash for e in evidence_by_id.values()})
    if coverage.matched > visited:
        gaps.append("Evidence retrieval did not inspect every enumerated document.")
    return {
        "evidence": sorted(evidence_by_id.values(), key=lambda e: e.id),
        "claims": [claims[key] for key in sorted(claims)],
        "coverage": coverage.model_copy(update={
            "visited": visited, "gaps": sorted(set(gaps)),
            "incomplete": coverage.incomplete or bool(cut_short),
        }),
        **({"audit": audit} if context.diagnostics else {}),
    }


def facet_audit(
    facet: str, status: str, result: WorkerResult | None, evidence: dict[str, Evidence],
) -> dict[str, object]:
    """Every claim a facet's worker returned, with its outcome and validation errors."""
    rounds = result.rounds if isinstance(result, TracedWorkerResult) else []
    seen = {pid for extraction in rounds for pid in extraction.passage_ids}
    shown = [item for pid, item in evidence.items() if pid in seen or not rounds]
    returned = [
        (n, claim, "capped" if capped else "") for n, extraction in enumerate(rounds)
        for capped, group in ((False, extraction.returned), (True, extraction.capped))
        for claim in group
    ] if rounds else [(0, claim, "") for claim in (result.claims if result else [])]
    entries = []
    for round_number, raw, outcome in returned:
        claim = anchor_claim(raw, shown)
        errors = claim_errors(claim, evidence)
        entries.append({
            **claim.model_dump(mode="json"), "round": round_number, "errors": errors,
            "anchored": claim != raw,
            "outcome": outcome or ("rejected" if errors else "accepted"),
        })
    return {
        "facet": facet, "status": status,
        "rounds": [
            {
                "query": extraction.query, "passage_ids": extraction.passage_ids,
                "gaps": extraction.gaps, "rewrite": extraction.rewrite,
            } for extraction in rounds
        ],
        "claims": entries,
    }


async def reconcile(
    state: ResearchState, runtime: Runtime[ResearchContext],
) -> dict[str, object]:
    found = conflicts(state["claims"])
    results: list[Calculation] = []
    claims = list(state["claims"])
    gaps = list(state["coverage"].gaps)

    def add_calculation(request: CalculationRequest) -> None:
        try:
            result = calculate(request, state["claims"])
            first_input = state["claims"][request.input_claims[0]]
            claims.append(Claim(
                text=f"Calculated {result.operation}: {result.value} {result.unit}",
                evidence_ids=result.evidence_ids, quote=first_input.quote,
                subject=first_input.subject, quantity=result.operation,
                value=result.value, unit=result.unit, date=first_input.date,
                conditions=first_input.conditions, scope=first_input.scope,
                calculation_index=len(results),
            ))
            results.append(result)
        except ValueError as error:
            logger.info("calculation_rejected reason=%s", error)
            gaps.append(str(error))

    for index, claim in enumerate(state["claims"]):
        if claim.unit == "kWh/sht":
            add_calculation(CalculationRequest(
                operation="convert", input_claims=[index], target_unit="kWh/t",
            ))
    if state["plan"].intent == "calculation" and state["claims"]:
        request = await runtime.context.models.calculation(
            state["question"], state["claims"], runtime.context.budget,
        )
        add_calculation(request)
    return {
        "conflicts": found, "calculations": results, "claims": claims,
        "coverage": state["coverage"].model_copy(update={"gaps": gaps}),
    }
