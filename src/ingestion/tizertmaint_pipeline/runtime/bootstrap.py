"""Wire Azure/PostgreSQL adapters and the stage registry from ``PipelineSettings``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from ..adapters.azure_ai.content_understanding import ContentUnderstandingClient
from ..adapters.azure_ai.cu_registry import load_registry, verify_registry
from ..adapters.azure_ai.document_intelligence import DocumentIntelligenceLayout
from ..adapters.azure_ai.embeddings import (
    AzureOpenAIEmbeddings,
    create_openai_client,
    utf8_token_upper_bound,
)
from ..adapters.azure_ai.remote_operation import ApiKeyFile
from ..adapters.blob.artifact_store import BlobArtifactStore
from ..adapters.blob.source_reader import BlobSourceReader
from ..adapters.parsers.calamine import CalamineSpreadsheetReader
from ..adapters.parsers.libredwg import ConverterLimits, LibreDwgReader
from ..adapters.parsers.pdfium import PdfiumToolkit
from ..adapters.parsers.tokenizers import TiktokenCounter, load_token_counter
from ..adapters.postgres.evidence import PostgresEvidenceStore
from ..adapters.postgres.ledger import PostgresLedger
from ..adapters.postgres.pool import create_pool
from ..adapters.postgres.snapshots import PostgresSnapshots
from ..adapters.search.projector import AzureSearchProjector
from ..adapters.servicebus.transport import ServiceBusSender, open_client
from ..adapters.system import SystemClock
from ..config import (
    BillingSettings,
    ExternalAccount,
    PipelineSettings,
    load_billing,
    load_policies,
    require,
)
from ..domain.cu import AnalyzerRegistry
from ..domain.errors import PipelineError
from ..domain.sniff import ZipLimits
from ..domain.spend import Prices
from ..domain.states import StageName
from ..ports.parsing import TokenCounter
from ..ports.providers import (
    ContentAnalysisProvider,
    EmbeddingProvider,
    LayoutProvider,
    SearchProjector,
)
from ..ports.telemetry import Telemetry
from ..stages.base import AccountClients, StageServices
from ..stages.chunk import ChunkStage
from ..stages.chunkers import ChunkingSettings
from ..stages.cu import AnalysisOptions, CuPollStage, CuSettings, CuSubmitStage
from ..stages.di import DiPollStage, DiSettings, DiSubmitStage, FigureAssetsStage
from ..stages.embed import EmbedStage
from ..stages.extract import NativeParseStage, NormalizeStage, ParserSettings
from ..stages.links import PlacementRouter
from ..stages.plan import LayoutOptions, PlanStage
from ..stages.preflight import PreflightStage
from ..stages.project_search import ProjectionSettings, ProjectSearchStage
from ..stages.snapshot import SnapshotStage
from ..stages.spend import BillingAccount, SpendPolicy
from .registry import StageRegistry

logger = logging.getLogger(__name__)


def spend_policy(billing: BillingSettings) -> SpendPolicy:
    prices = None
    if billing.di_page_usd is not None and billing.cu_page_estimate_usd is not None:
        prices = Prices(
            di_page_usd=billing.di_page_usd,
            cu_page_estimate_usd=billing.cu_page_estimate_usd,
            cu_meter_usd=dict(billing.cu_meter_usd),
        )
    accounts = (
        BillingAccount(budget_usd=billing.budget_usd),
        *(
            BillingAccount(
                account.name,
                account.budget_usd,
                cu_max_inflight=account.cu_max_inflight,
                di_max_inflight=account.di_max_inflight,
            )
            for account in billing.accounts
        ),
    )
    return SpendPolicy(accounts=accounts, prices=prices)


def build_registry(settings: PipelineSettings) -> StageRegistry:
    """Every process registers every stage so successor jobs get consistent versions."""

    spend = spend_policy(load_billing(settings.billing_path))
    di = settings.di
    di_settings = DiSettings(
        max_source_bytes=settings.blob.max_source_bytes,
        max_inflight=di.max_inflight,
        page_budget=di.page_budget,
        poll_schedule=di.poll_schedule_seconds,
        poll_deadline_seconds=di.poll_deadline_seconds,
        result_retention_seconds=di.result_retention_seconds,
        spend=spend,
    )
    cu = settings.cu
    cu_settings = CuSettings(
        max_source_bytes=settings.blob.max_source_bytes,
        max_inflight=cu.max_inflight,
        page_budget=cu.page_budget,
        token_budget=cu.token_budget,
        est_tokens_per_page=cu.est_tokens_per_page,
        poll_schedule=cu.poll_schedule_seconds,
        poll_deadline_seconds=cu.poll_deadline_seconds,
        capture_figures=cu.capture_figures,
        spend=spend,
    )
    analysis = AnalysisOptions(processing_location=cu.processing_location)
    policies = load_policies(settings.policies_path)
    parsers = settings.parsers
    chunking = settings.chunking
    search = settings.search
    return (
        StageRegistry()
        .register(
            StageName.SNAPSHOT,
            SnapshotStage(
                max_source_bytes=settings.blob.max_source_bytes,
                successors=PlacementRouter(settings.default_routing()),
            ),
        )
        .register(
            StageName.PREFLIGHT,
            PreflightStage(
                max_source_bytes=settings.blob.max_source_bytes,
                zip_limits=ZipLimits(
                    max_entries=parsers.zip_max_entries,
                    max_uncompressed_bytes=parsers.zip_max_uncompressed_bytes,
                    max_ratio=parsers.zip_max_ratio,
                ),
            ),
        )
        .register(
            StageName.PLAN,
            PlanStage(
                LayoutOptions(
                    model_id=di.model_id,
                    figures=di.figures,
                    features=di.features,
                    drawing_features=di.drawing_features,
                    locale=di.locale,
                ),
                analysis,
                max_source_bytes=settings.blob.max_source_bytes,
            ),
        )
        .register(
            StageName.NATIVE_PARSE,
            NativeParseStage(
                ParserSettings(
                    max_source_bytes=settings.blob.max_source_bytes,
                    max_text_bytes=parsers.max_text_bytes,
                    include_hidden_sheets=parsers.include_hidden_sheets,
                    max_cells=parsers.max_cells,
                )
            ),
        )
        .register(StageName.DI_SUBMIT, DiSubmitStage(di_settings))
        .register(StageName.DI_POLL, DiPollStage(di_settings))
        .register(StageName.CU_SUBMIT, CuSubmitStage(cu_settings))
        .register(StageName.CU_POLL, CuPollStage(cu_settings))
        .register(
            StageName.FIGURE_ASSETS,
            FigureAssetsStage(di_settings, cu_figure_path=cu.figure_path_template),
        )
        .register(
            StageName.NORMALIZE,
            NormalizeStage(
                max_result_bytes=max(di.max_result_bytes, cu.max_result_bytes),
                analysis=analysis,
            ),
        )
        .register(
            StageName.CHUNK,
            ChunkStage(
                ChunkingSettings(
                    min_tokens=chunking.min_tokens,
                    max_tokens=chunking.max_tokens,
                    overlap_tokens=chunking.overlap_tokens,
                    table_max_tokens=chunking.table_max_tokens,
                    allow_partial_projection=chunking.allow_partial_projection,
                    scientific_observations=chunking.scientific_observations,
                )
            ),
        )
        .register(StageName.EMBED, EmbedStage(batch_size=chunking.embed_batch_size))
        .register(
            StageName.PROJECT_SEARCH,
            ProjectSearchStage(
                ProjectionSettings(
                    target=search.index_name,
                    corpus=search.corpus,
                    release_id=search.release_id,
                    access_scope_id=search.access_scope_id,
                    acl_policy_version=search.acl_policy_version,
                    verify_deadline_seconds=search.verify_deadline_seconds,
                    retry_seconds=search.retry_seconds,
                    release_ids=policies.release_ids(),
                )
            ),
        )
    )


@dataclass
class AzureRuntime:
    settings: PipelineSettings
    telemetry: Telemetry
    credential: Any
    pool: Any
    ledger: PostgresLedger
    registry: StageRegistry
    services: StageServices
    source_container: Any | None
    bus_client: Any | None
    sender: ServiceBusSender | None


@asynccontextmanager
async def azure_runtime(
    settings: PipelineSettings,
    telemetry: Telemetry,
    *,
    blob: bool = True,
    bus: bool = True,
    analyzers: bool = False,
) -> AsyncIterator[AzureRuntime]:
    """``analyzers`` reads and verifies the CU analyzer catalog (stage workers only)."""

    from azure.identity.aio import DefaultAzureCredential

    async with AsyncExitStack() as stack:
        credential = await stack.enter_async_context(DefaultAzureCredential())
        pool = await create_pool(settings.db, credential)
        stack.push_async_callback(pool.close)
        ledger = PostgresLedger(pool)

        source_container = sources = artifacts = None
        if blob:
            from azure.storage.blob.aio import BlobServiceClient

            service = await stack.enter_async_context(
                BlobServiceClient(
                    require(settings.blob.account_url, "TIZ_BLOB__ACCOUNT_URL"), credential
                )
            )
            source_container = service.get_container_client(
                require(settings.blob.source_container, "TIZ_BLOB__SOURCE_CONTAINER")
            )
            sources = BlobSourceReader(source_container)
            artifacts = BlobArtifactStore(
                service.get_container_client(settings.blob.artifact_container),
                max_bytes=settings.blob.max_artifact_bytes,
            )

        bus_client = sender = None
        if bus:
            bus_client = await stack.enter_async_context(
                open_client(require(settings.bus.namespace, "TIZ_BUS__NAMESPACE"), credential)
            )
            sender = ServiceBusSender(bus_client, settings.bus.queues)
            stack.push_async_callback(sender.close)

        registry = build_registry(settings)
        evidence = PostgresEvidenceStore(pool)
        content_analysis = _content_analysis(settings, settings.cu.endpoint, credential, stack)
        catalog = None
        if analyzers and content_analysis is not None:
            catalog = await _verified_catalog(settings, content_analysis)
        tokens = load_token_counter()
        services = StageServices(
            clock=SystemClock(),
            planner=registry.planner(max_failures=settings.runtime.max_failures),
            snapshots=PostgresSnapshots(pool),
            sources=sources,
            artifacts=artifacts,
            plans=ledger,
            workload=ledger,
            work_dir=settings.parsers.work_dir,
            pdf=PdfiumToolkit(),
            sheets=CalamineSpreadsheetReader(),
            drawings=LibreDwgReader(
                ConverterLimits(
                    timeout_seconds=settings.parsers.dwg_timeout_seconds,
                    memory_bytes=settings.parsers.dwg_memory_bytes,
                )
            ),
            tokens=tokens,
            layout=_layout(settings, settings.di.endpoint, credential, stack),
            evidence=evidence,
            embedding_cache=evidence,
            projections=evidence,
            embeddings=_embeddings(settings, credential, stack, tokens),
            projector=await _projector(settings, credential, stack),
            content_analysis=content_analysis,
            analyzers=catalog,
            extraction_cache=evidence,
            accounts=await _external_accounts(settings, stack, catalog),
        )
        yield AzureRuntime(
            settings=settings,
            telemetry=telemetry,
            credential=credential,
            pool=pool,
            ledger=ledger,
            registry=registry,
            services=services,
            source_container=source_container,
            bus_client=bus_client,
            sender=sender,
        )


def _layout(
    settings: PipelineSettings, endpoint: str | None, credential: Any, stack: AsyncExitStack
) -> LayoutProvider | None:
    di = settings.di
    if not endpoint:
        return None
    layout = DocumentIntelligenceLayout(
        endpoint=endpoint,
        credential=credential,
        api_version=di.api_version,
        max_result_bytes=min(di.max_result_bytes, settings.blob.max_artifact_bytes),
        max_figure_bytes=di.max_figure_bytes,
    )
    stack.push_async_callback(layout.close)
    return layout


def _content_analysis(
    settings: PipelineSettings, endpoint: str | None, credential: Any, stack: AsyncExitStack
) -> ContentAnalysisProvider | None:
    cu = settings.cu
    if not endpoint:
        return None
    client = ContentUnderstandingClient(
        endpoint=endpoint,
        credential=credential,
        api_version=cu.api_version,
        max_result_bytes=min(cu.max_result_bytes, settings.blob.max_artifact_bytes),
        max_file_bytes=cu.max_file_bytes,
    )
    stack.push_async_callback(client.close)
    return client


async def _verified_catalog(
    settings: PipelineSettings, client: ContentAnalysisProvider
) -> AnalyzerRegistry:
    path = require(settings.cu.registry_path, "TIZ_CU__REGISTRY_PATH")
    check = await verify_registry(client, await load_registry(path))
    for name, problems in sorted(check.problems.items()):
        logger.warning(
            "CU analyzer %s failed verification and is disabled",
            name,
            extra={"analyzer_id": name, "problems": problems[:20]},
        )
    logger.info("CU analyzers verified", extra={"analyzers": check.verified})
    return check.catalog


async def _external_accounts(
    settings: PipelineSettings, stack: AsyncExitStack, catalog: AnalyzerRegistry | None
) -> dict[str, AccountClients]:
    """Clients of every external account whose key is mounted in this process."""

    clients: dict[str, AccountClients] = {}
    for account in load_billing(settings.billing_path).accounts:
        if not account.key_file.is_file():
            logger.info(
                "Billing account %s has no key file here; this process does not use it",
                account.name,
                extra={"account": account.name},
            )
            continue
        key = ApiKeyFile(account.key_file)
        analysis = _content_analysis(settings, account.cu_endpoint, key, stack)
        matching: frozenset[str] = frozenset()
        if analysis is not None and catalog is not None:
            matching = await _matching_analyzers(settings, account, analysis, catalog)
        clients[account.name] = AccountClients(
            layout=_layout(settings, account.di_endpoint, key, stack),
            content_analysis=analysis,
            analyzers=matching,
        )
    return clients


async def _matching_analyzers(
    settings: PipelineSettings,
    account: ExternalAccount,
    client: ContentAnalysisProvider,
    catalog: AnalyzerRegistry,
) -> frozenset[str]:
    """Analyzers the account serves exactly as this subscription does (same profile)."""

    path = require(settings.cu.registry_path, "TIZ_CU__REGISTRY_PATH")
    try:
        check = await verify_registry(client, await load_registry(path))
    except PipelineError as error:
        logger.error(
            "CU analyzers of billing account %s could not be verified; no new CU work goes there",
            account.name,
            extra={"account": account.name, "error": str(error)[:500]},
        )
        return frozenset()
    matching = frozenset(
        name
        for name, profile in check.catalog.profiles.items()
        if name in catalog.profiles and catalog.profiles[name].identity() == profile.identity()
    )
    logger.info(
        "CU analyzers of billing account %s verified",
        account.name,
        extra={
            "account": account.name,
            "analyzers": sorted(matching),
            "not_matching": sorted(set(catalog.profiles) - matching),
            "problems": {name: found[:5] for name, found in check.problems.items()},
        },
    )
    return matching


def embedding_token_counter(tokens: TokenCounter) -> Callable[[str], int]:
    """The chunker's exact tokenizer, else the UTF-8 bound (an approximation could undercount)."""

    return tokens.count if isinstance(tokens, TiktokenCounter) else utf8_token_upper_bound


def _embeddings(
    settings: PipelineSettings, credential: Any, stack: AsyncExitStack, tokens: TokenCounter
) -> EmbeddingProvider | None:
    config = settings.embeddings
    if not (config.endpoint and config.deployment):
        return None
    client = create_openai_client(config.endpoint, credential, api_version=config.api_version)
    stack.push_async_callback(client.close)
    return AzureOpenAIEmbeddings(
        client,
        deployment=config.deployment,
        model=config.model,
        dimensions=config.dimensions,
        max_inputs_per_request=config.max_inputs_per_request,
        max_tokens_per_request=config.max_tokens_per_request,
        max_tokens_per_input=config.max_tokens_per_input,
        token_counter=embedding_token_counter(tokens),
    )


async def _projector(
    settings: PipelineSettings, credential: Any, stack: AsyncExitStack
) -> SearchProjector | None:
    search = settings.search
    if not search.endpoint:
        return None
    from azure.search.documents.aio import SearchClient

    client = await stack.enter_async_context(
        SearchClient(search.endpoint, search.index_name, credential)
    )
    return AzureSearchProjector(
        client,
        max_batch_documents=search.max_batch_documents,
        max_batch_bytes=search.max_batch_bytes,
    )
