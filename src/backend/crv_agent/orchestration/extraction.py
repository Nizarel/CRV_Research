"""Bounded retrieve-then-extract evidence worker (default).

Per facet: retrieve the focused candidates and expose them under the measured EvidencePolicy
(by default all 50 as full chunks). One structured LangChain extraction call then returns
claims, gaps and an optional rewrite query. At most one rewrite round follows, showing only
candidates not already shown. Each passage is sent to the model exactly once, so token use
is bounded (about 1-2 model calls per facet) instead of growing with an agent loop.
"""

import asyncio
import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from crv_agent.domain.evidence import (
    Claim,
    Evidence,
    ExtractionRound,
    TracedWorkerResult,
    WorkerResult,
)
from crv_agent.domain.execution import Budget, estimate_tokens
from crv_agent.domain.planning import Contract
from crv_agent.orchestration.middleware import UsageRecorder
from crv_agent.orchestration.prompts import EXTRACT_RULES
from crv_agent.ports.rerank import Reranker
from crv_agent.services.candidates import ranked_candidates
from crv_agent.services.evidence_pack import EvidencePolicy, build_pack, expose, merge_exposure
from crv_agent.services.retrieval import Retrieval, identifiers

MAX_ROUNDS = 2
# Research never spends the tokens the answer stage needs (compose, verify, one repair).
ANSWER_HEADROOM_TOKENS = 60_000


class Extraction(Contract):
    claims: list[Claim]
    gaps: list[str]
    rewrite: str | None


class ExtractionWorker:
    def __init__(
        self, model: BaseChatModel, retrieval: Retrieval, output_tokens: int,
        policy: EvidencePolicy | None = None, reranker: Reranker | None = None,
        max_claims: int = 8, batches: int = 1,
    ) -> None:
        self.model, self.retrieval, self.output_tokens = model, retrieval, output_tokens
        self.policy, self.reranker = policy or EvidencePolicy(), reranker
        self.max_claims, self.batches = max_claims, batches
        self.schema = json.dumps(Extraction.model_json_schema())

    async def run(
        self, question: str, release: str, hashes: list[str], budget: Budget,
    ) -> tuple[WorkerResult, list[Evidence]]:
        fixed = identifiers(question)
        found: dict[str, Evidence] = {}
        claims: list[Claim] = []
        gaps: list[str] = []
        rounds: list[ExtractionRound] = []
        query = question.split("\n", 1)[0]
        for round_number in range(MAX_ROUNDS):
            text = (query + "\n" + " ".join(fixed)).strip()
            hits = await ranked_candidates(
                self.retrieval, text, release, hashes, budget, self.policy, self.reranker,
            )
            fresh = [hit for hit in hits if hit.id not in found]
            pack = build_pack(fresh, text, fixed, self.policy)
            if not pack.shown:
                gaps.append("A rewritten search returned no new candidates.")
                break
            passages: list[dict[str, object]] = []
            for hit, span in pack.shown:
                visible = expose(hit, span)
                found[hit.id] = (
                    merge_exposure(found[hit.id], visible) if hit.id in found else visible
                )
                passages.append({
                    "id": hit.id, "title": hit.title, "source": hit.source_path or hit.source_uri,
                    "document_code": hit.document_code, "pages": hit.pages,
                    "source_role": hit.source_role, "citation_basis": hit.citation_basis,
                    "truncated": span != (0, len(hit.content)),
                    "text": hit.content[span[0]:span[1]],
                })
            # Round-robin batches mix top- and lower-ranked passages; each is extracted in
            # parallel, so one call no longer has to find every fact in ~50 full chunks.
            groups = [
                group for i in range(self.batches) if (group := passages[i::self.batches])
            ]
            results = await asyncio.gather(*(
                self.extract(question, group, round_number, claims, budget) for group in groups
            ), return_exceptions=True)
            extractions = [result for result in results if isinstance(result, Extraction)]
            if not extractions:
                # Re-raise the original type so BudgetExceeded keeps its facet status.
                raise next(r for r in results if isinstance(r, BaseException))
            if len(extractions) < len(results):
                gaps.append("An extraction batch failed; some retrieved passages were not read.")
            kept = [claim for e in extractions for claim in e.claims[:self.max_claims]]
            fresh_claims = {claim.model_dump_json(): claim for claim in kept}
            claims.extend(fresh_claims.values())
            # A batch reports aspects missing from its own passages; keep them only when no
            # batch found anything, otherwise they contradict the other batches' claims.
            if not fresh_claims or len(extractions) == 1:
                gaps.extend(gap for extraction in extractions for gap in extraction.gaps)
            rewrite = next((e.rewrite for e in extractions if e.rewrite), None)
            rounds.append(ExtractionRound(
                query=text, passage_ids=[str(passage["id"]) for passage in passages],
                returned=kept,
                capped=[claim for e in extractions for claim in e.claims[self.max_claims:]],
                gaps=[gap for e in extractions for gap in e.gaps], rewrite=rewrite,
            ))
            # Rewrite only when nothing usable was found: a second sequential round costs
            # another full model call on the critical path.
            if fresh_claims or not rewrite or round_number + 1 >= MAX_ROUNDS:
                break
            query = rewrite
        gaps.append("Top-50 candidate search per query; not an exhaustive document review.")
        return TracedWorkerResult(
            claims=claims, gaps=list(dict.fromkeys(gaps)), rounds=rounds,
        ), list(found.values())

    async def extract(
        self, question: str, passages: list[dict[str, object]], round_number: int,
        previous: list[Claim], budget: Budget,
    ) -> Extraction:
        data = json.dumps({
            "facet": question, "round": round_number + 1, "max_rounds": MAX_ROUNDS,
            "max_claims": self.max_claims,
            "claims_already_found": [c.text for c in previous], "passages": passages,
        }, ensure_ascii=False)
        await budget.reserve(model=1, tokens=(
            estimate_tokens(EXTRACT_RULES + data + self.schema) + self.output_tokens
        ), headroom=ANSWER_HEADROOM_TOKENS)
        result = await budget.wait(self.model.with_structured_output(
            Extraction, method="json_schema", strict=True,
        ).ainvoke(
            [SystemMessage(content=EXTRACT_RULES), HumanMessage(content=data)],
            {"callbacks": [UsageRecorder(budget, "worker", "evidence_extractor")]},
        ))
        return Extraction.model_validate(result)
