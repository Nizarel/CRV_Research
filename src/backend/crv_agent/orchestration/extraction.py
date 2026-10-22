"""Bounded retrieve-then-extract evidence worker (default).

Per facet: retrieve the focused candidates and expose them under the measured EvidencePolicy
(by default all 50 as full chunks). One structured LangChain extraction call then returns
claims, gaps and an optional rewrite query. At most one rewrite round follows, showing only
candidates not already shown. Each passage is sent to the model exactly once, so token use
is bounded (about 1-2 model calls per facet) instead of growing with an agent loop.
"""

import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from crv_agent.domain.evidence import Claim, Evidence, WorkerResult
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
    ) -> None:
        self.model, self.retrieval, self.output_tokens = model, retrieval, output_tokens
        self.policy, self.reranker = policy or EvidencePolicy(), reranker
        self.schema = json.dumps(Extraction.model_json_schema())

    async def run(
        self, question: str, release: str, hashes: list[str], budget: Budget,
    ) -> tuple[WorkerResult, list[Evidence]]:
        fixed = identifiers(question)
        found: dict[str, Evidence] = {}
        claims: list[Claim] = []
        gaps: list[str] = []
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
            extraction = await self.extract(question, passages, round_number, claims, budget)
            claims.extend(extraction.claims)
            gaps.extend(extraction.gaps)
            # Rewrite only when nothing usable was found: a second sequential round costs
            # another full model call on the critical path.
            if extraction.claims or not extraction.rewrite or round_number + 1 >= MAX_ROUNDS:
                break
            query = extraction.rewrite
        gaps.append("Top-50 candidate search per query; not an exhaustive document review.")
        return WorkerResult(claims=claims, gaps=list(dict.fromkeys(gaps))), list(found.values())

    async def extract(
        self, question: str, passages: list[dict[str, object]], round_number: int,
        previous: list[Claim], budget: Budget,
    ) -> Extraction:
        data = json.dumps({
            "facet": question, "round": round_number + 1, "max_rounds": MAX_ROUNDS,
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
