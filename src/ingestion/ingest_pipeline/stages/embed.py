"""Embed chunk text (cache first) and plan the projection of the content's rows."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.chunks import EvidenceChunk
from ..domain.errors import IntegrityError
from ..domain.models import ProviderUsage, StageJob, StageOutcome
from ..ports.evidence import EvidenceStatus
from .base import StageContext
from .chunk import EmbedInputs
from .common import load_jsonl, parse_inputs
from .links import project_job

# 2: the provider's input limit counts with the chunker's tokenizer (quarantined inputs can rerun).
STAGE_VERSION = "2"
PROVIDER = "azure_openai_embeddings"


@dataclass(frozen=True)
class EmbedStage:
    batch_size: int = 64

    @property
    def version(self) -> str:
        return STAGE_VERSION

    async def execute(self, context: StageContext, job: StageJob) -> StageOutcome:
        inputs = parse_inputs(EmbedInputs, job)
        services = context.services
        repository = services.require_evidence()
        record = await repository.get_document(inputs.evidence_id)
        if record is None:
            raise IntegrityError(f"Evidence {inputs.evidence_id} is not recorded")
        embedded = requests = 0
        tokens = 0
        if record.status is not EvidenceStatus.EMBEDDED:
            provider = services.require_embeddings()
            cache = services.require_embedding_cache()
            chunks = await load_jsonl(services, record.chunk_set, EvidenceChunk)
            texts = {chunk.text_sha256: chunk for chunk in chunks}
            cached = await cache.get_many(
                list(texts), model=provider.model, dimensions=provider.dimensions
            )
            missing = [key for key in texts if key not in cached]
            for start in range(0, len(missing), self.batch_size):
                batch = missing[start : start + self.batch_size]
                vectors = await provider.embed([texts[key].text for key in batch])
                if len(vectors) != len(batch):
                    raise IntegrityError("Embedding provider returned a wrong vector count")
                # Persist per batch: a retry resumes from the cache, never pays twice.
                await cache.put_many(
                    dict(zip(batch, vectors, strict=True)),
                    model=provider.model,
                    dimensions=provider.dimensions,
                    now=services.clock.now(),
                )
                embedded += len(batch)
                requests += 1
                tokens += sum(texts[key].token_count for key in batch)
            # Mark before projecting: a placement added meanwhile then sees embedded evidence.
            await repository.mark_embedded(inputs.evidence_id, now=services.clock.now())
        return StageOutcome.succeeded(
            next_jobs=(
                project_job(
                    services.planner,
                    content_hash=inputs.content_hash,
                    policy_version=inputs.policy_version,
                    evidence_id=inputs.evidence_id,
                ),
            ),
            usage=(
                ProviderUsage(provider=PROVIDER, input_tokens=tokens, requests=requests)
                if requests
                else None
            ),
            checkpoint={"embedded": embedded},
        )
