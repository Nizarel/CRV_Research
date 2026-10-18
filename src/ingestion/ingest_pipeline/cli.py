"""``tizagent-pipeline`` command line: migrations, planning, coordinator and workers.

Configuration comes from ``TIZ_*`` environment variables (see ``config.py``); Azure access
uses ``DefaultAzureCredential`` (workload identity in AKS).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, nullcontext
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .adapters.azure_ai.content_understanding import ContentUnderstandingClient
from .adapters.azure_ai.cu_registry import load_registry, verify_registry
from .adapters.blob.intake import publish_intake
from .adapters.local_intake import CorpusIntake
from .adapters.parsers.office import OfficeConverter
from .adapters.postgres.kg_access import ensure_entra_login, grant_reader
from .adapters.postgres.knowledge import PostgresKnowledgeStore
from .adapters.postgres.legacy import LegacyCatalog
from .adapters.postgres.migrations import apply_migrations, load_migrations
from .adapters.postgres.pool import create_pool
from .adapters.postgres.report import pipeline_status
from .adapters.servicebus.transport import ServiceBusMessageReceiver, open_receiver
from .adapters.telemetry.json_logging import configure_logging
from .adapters.telemetry.otel import NoopTelemetry, configure_telemetry
from .config import PipelineSettings, load_billing, load_policies, require
from .domain.ids import legacy_placement_id, legacy_source_object_id
from .domain.intake import IntakeLimits
from .domain.knowledge import (
    DEFAULT_RELEASE_STATUSES,
    STATUS_RANK,
    parse_aliases,
    parse_registry,
)
from .domain.models import JobSpec
from .domain.states import StageName, WorkerPool
from .ports.telemetry import Telemetry
from .runtime.bootstrap import azure_runtime, spend_policy
from .runtime.coordinator import Coordinator, LeaseReconciler, OutboxDispatcher
from .runtime.executor import ExecutorSettings, StageExecutor
from .runtime.shutdown import install_stop_signals
from .runtime.worker import Worker
from .stages.links import preflight_job, project_job
from .stages.snapshot import snapshot_job

Handler = Callable[[PipelineSettings, Telemetry, argparse.Namespace], Coroutine[Any, Any, int]]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
ENQUEUE_CHUNK = 500


async def _prepare_corpus(
    settings: PipelineSettings, _: Telemetry, args: argparse.Namespace
) -> int:
    limits = IntakeLimits(
        max_source_bytes=settings.blob.max_source_bytes,
        max_total_bytes=args.max_total_gib * 1024**3,
    )
    converter = (
        OfficeConverter(args.office_image, max_output_bytes=limits.max_source_bytes)
        if args.office_image else None
    )
    report = await asyncio.to_thread(
        CorpusIntake(Path(args.output), limits, converter).prepare, Path(args.root)
    )
    _print({
        "manifest": str(Path(args.output) / "manifest.json"),
        "complete": report.complete, "outcomes": dict(Counter(i.status for i in report.items)),
    })
    return 2 if report.failed else 0


async def _ingest_corpus(
    settings: PipelineSettings, telemetry: Telemetry, args: argparse.Namespace
) -> int:
    count = 0
    async with azure_runtime(settings, telemetry, bus=False) as runtime:
        container = require(runtime.source_container, "TIZ_BLOB__SOURCE_CONTAINER")
        async for name, etag, digest in publish_intake(
            container, Path(args.manifest), prefix=args.prefix,
            max_source_bytes=settings.blob.max_source_bytes,
        ):
            job = snapshot_job(
                runtime.services.planner, container_name=container.container_name,
                container_url=container.url, blob_name=name, etag=etag, content_hash=digest,
            )
            await runtime.ledger.enqueue([job], now=runtime.services.clock.now())
            count += 1
    _print({"sources": count, "prefix": args.prefix})
    return 0


def _print(payload: Any) -> None:
    print(json.dumps(payload, default=str, sort_keys=True))


def _read_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


async def _migrate(settings: PipelineSettings, _: Telemetry, args: argparse.Namespace) -> int:
    async with _database(settings) as pool, pool.acquire() as connection:
        report = await apply_migrations(connection, load_migrations(), dry_run=args.dry_run)
    _print(asdict(report))
    return 0


@asynccontextmanager
async def _database(settings: PipelineSettings) -> AsyncIterator[Any]:
    """A pool only (no blob, bus or providers): Entra token unless a test DSN is set."""

    if settings.db.dsn is None:
        from azure.identity.aio import DefaultAzureCredential

        credential_context: Any = DefaultAzureCredential()
    else:
        credential_context = nullcontext(None)
    async with credential_context as credential:
        pool = await create_pool(settings.db, credential)
        try:
            yield pool
        finally:
            await pool.close()


async def _kg_import(settings: PipelineSettings, _: Telemetry, args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    batch = parse_registry(await asyncio.to_thread(_read_bytes, args.registry))
    async with _database(settings) as pool:
        report = await PostgresKnowledgeStore(pool).import_registry(
            batch, started_at=started, now=datetime.now(UTC)
        )
    _print(
        {
            **asdict(report),
            "input_hash": batch.input_hash,
            "entities_in_file": len(batch.entities),
            "assertions_in_file": len(batch.assertions),
        }
    )
    return 0


async def _kg_aliases(settings: PipelineSettings, _: Telemetry, args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    aliases = parse_aliases(await asyncio.to_thread(_read_bytes, args.aliases))
    async with _database(settings) as pool:
        report = await PostgresKnowledgeStore(pool).import_aliases(
            aliases, started_at=started, now=datetime.now(UTC)
        )
    _print({**asdict(report), "input_hash": aliases.input_hash})
    return 0


async def _kg_release(settings: PipelineSettings, _: Telemetry, args: argparse.Namespace) -> int:
    started = datetime.now(UTC)
    statuses = tuple(args.status or DEFAULT_RELEASE_STATUSES)
    async with _database(settings) as pool:
        store = PostgresKnowledgeStore(pool)
        release = await store.build_release(
            statuses=statuses, started_at=started, now=datetime.now(UTC)
        )
        graph = (
            await store.load_age(release.release_id, now=datetime.now(UTC))
            if args.load_age
            else None
        )
        if args.activate:
            await store.activate(
                release.release_id, environment=args.activate, now=datetime.now(UTC)
            )
    _print({**asdict(release), "age_graph": graph, "activated": args.activate})
    return 0


async def _kg_grant_reader(
    settings: PipelineSettings, _: Telemetry, args: argparse.Namespace
) -> int:
    # Entra principals are created from the server's `postgres` database.
    admin = settings.model_copy(
        update={"db": settings.db.model_copy(update={"database": "postgres"})}
    )
    async with _database(admin) as pool, pool.acquire() as connection:
        created = await ensure_entra_login(connection, args.login, args.object_id)
    async with _database(settings) as pool, pool.acquire() as connection:
        report = await grant_reader(
            connection, args.login, statement_timeout_ms=args.statement_timeout_ms
        )
    _print({"login": args.login, "created": created, **report})
    return 0


def _declared_hash(metadata: dict[str, str] | None) -> str | None:
    value = (metadata or {}).get("content_hash")
    return value if value and _SHA256.match(value) else None


def read_blob_list(path: str) -> list[str]:
    """UTF-8 file with one blob name per line (no shell or console encoding involved)."""

    with open(path, encoding="utf-8-sig") as handle:
        names = [line.rstrip("\r\n") for line in handle]
    names = [name for name in names if name.strip() and not name.startswith("#")]
    if not names:
        raise SystemExit(f"{path} lists no blob names")
    return list(dict.fromkeys(names))


async def _enqueue_snapshots(
    settings: PipelineSettings, telemetry: Telemetry, args: argparse.Namespace
) -> int:
    async with azure_runtime(settings, telemetry, bus=False) as runtime:
        container = require(runtime.source_container, "TIZ_BLOB__SOURCE_CONTAINER")
        planner = runtime.services.planner
        specs: list[JobSpec] = []

        def plan(name: str, etag: str, metadata: dict[str, str] | None) -> None:
            specs.append(
                snapshot_job(
                    planner,
                    container_name=container.container_name,
                    container_url=container.url,
                    blob_name=name,
                    etag=etag,
                    content_hash=_declared_hash(metadata),
                    priority=args.priority,
                )
            )

        names = list(args.blob_name or [])
        if args.blob_list:
            names.extend(read_blob_list(args.blob_list))
        if names:
            for name in names:
                properties = await container.get_blob_client(name).get_blob_properties()
                plan(name, properties.etag, properties.metadata)
        else:
            async for blob in container.list_blobs(
                name_starts_with=args.prefix, include=["metadata"]
            ):
                plan(blob.name, blob.etag, blob.metadata)
                if args.limit is not None and len(specs) >= args.limit:
                    break

        inserted = 0
        now = runtime.services.clock.now()
        for start in range(0, len(specs), ENQUEUE_CHUNK):
            inserted += len(
                await runtime.ledger.enqueue(specs[start : start + ENQUEUE_CHUNK], now=now)
            )
    _print({"planned": len(specs), "inserted": inserted, "already_known": len(specs) - inserted})
    return 0


async def _enqueue_arm(
    settings: PipelineSettings, telemetry: Telemetry, args: argparse.Namespace
) -> int:
    """Plan already-snapshotted blobs under a named routing policy (one bake-off arm)."""

    entry = load_policies(require(settings.policies_path, "TIZ_POLICIES_PATH")).get(args.policy)
    names = list(args.blob_name or [])
    if args.blob_list:
        names.extend(read_blob_list(args.blob_list))
    async with azure_runtime(settings, telemetry, bus=False) as runtime:
        container = require(runtime.source_container, "TIZ_BLOB__SOURCE_CONTAINER")
        snapshots = runtime.services.require_snapshots()
        planner = runtime.services.planner
        specs: list[JobSpec] = []
        missing: list[str] = []
        contents: set[str] = set()
        for name in dict.fromkeys(names):
            placement = legacy_placement_id(legacy_source_object_id(container.url, name), name)
            snapshot = await snapshots.latest_for_placement(placement)
            if snapshot is None:
                missing.append(name)
                continue
            if snapshot.content_hash not in contents:
                contents.add(snapshot.content_hash)
                specs.append(preflight_job(planner, snapshot.content_hash, entry.policy))
                specs.append(
                    project_job(
                        planner,
                        content_hash=snapshot.content_hash,
                        policy_version=entry.policy.version,
                    )
                )
        inserted = await runtime.ledger.enqueue(specs, now=runtime.services.clock.now())
    _print(
        {
            "policy": args.policy,
            "release_id": entry.release_id,
            "contents": len(contents),
            "planned": len(specs),
            "inserted": len(inserted),
            "not_snapshotted": missing,
        }
    )
    return 1 if missing else 0


async def _cu_check(
    settings: PipelineSettings, _: Telemetry, args: argparse.Namespace
) -> int:
    """Read-only: verify model defaults and every reviewed analyzer (no analysis submitted)."""

    from azure.identity.aio import DefaultAzureCredential

    cu = settings.cu
    registry = await load_registry(require(cu.registry_path, "TIZ_CU__REGISTRY_PATH"))
    async with DefaultAzureCredential() as credential:
        client = ContentUnderstandingClient(
            endpoint=require(cu.endpoint, "TIZ_CU__ENDPOINT"),
            credential=credential,
            api_version=cu.api_version,
            max_result_bytes=cu.max_result_bytes,
        )
        try:
            check = await verify_registry(client, registry)
        finally:
            await client.close()
    _print(
        {
            "api_version": registry.api_version,
            "verified": check.verified,
            "problems": check.problems,
            "profiles": {
                name: profile.identity() for name, profile in check.catalog.profiles.items()
            },
        }
    )
    return 1 if check.problems else 0


async def _coordinator(
    settings: PipelineSettings, telemetry: Telemetry, _: argparse.Namespace
) -> int:
    runtime_settings = settings.runtime
    async with azure_runtime(settings, telemetry, blob=False) as runtime:
        clock = runtime.services.clock
        dispatcher = OutboxDispatcher(
            outbox=runtime.ledger,
            sender=require(runtime.sender, "Service Bus sender"),
            clock=clock,
            telemetry=telemetry,
            batch_size=runtime_settings.dispatch_batch_size,
            claim_seconds=runtime_settings.dispatch_claim_seconds,
            retry_policy=runtime_settings.retry_policy(),
        )
        accounts = spend_policy(load_billing(settings.billing_path)).accounts
        reconciler = LeaseReconciler(
            ledger=runtime.ledger,
            clock=clock,
            telemetry=telemetry,
            batch_size=runtime_settings.reconcile_batch_size,
            stale_dispatch_seconds=runtime_settings.stale_dispatch_seconds,
            remote_limits={
                StageName.CU_SUBMIT: settings.cu.max_inflight
                + sum(account.cu_max_inflight or 0 for account in accounts),
                StageName.DI_SUBMIT: settings.di.max_inflight
                + sum(account.di_max_inflight or 0 for account in accounts),
            },
        )
        stop = asyncio.Event()
        install_stop_signals(stop)
        await Coordinator(
            dispatcher=dispatcher,
            reconciler=reconciler,
            dispatch_interval_seconds=runtime_settings.dispatch_interval_seconds,
            reconcile_interval_seconds=runtime_settings.reconcile_interval_seconds,
            dispatch_batch_size=runtime_settings.dispatch_batch_size,
        ).run(stop)
    return 0


async def _worker(
    settings: PipelineSettings, telemetry: Telemetry, args: argparse.Namespace
) -> int:
    runtime_settings = settings.runtime
    pool = WorkerPool(args.pool)
    async with azure_runtime(settings, telemetry, analyzers=True) as runtime:
        sdk_receiver, renewer = open_receiver(
            runtime.bus_client,
            settings.bus.queue_for(pool),
            max_lock_renewal_seconds=settings.bus.max_lock_renewal_seconds,
        )
        try:
            async with sdk_receiver:
                executor = StageExecutor(
                    ledger=runtime.ledger,
                    registry=runtime.registry,
                    services=runtime.services,
                    policy=runtime_settings.retry_policy(),
                    settings=ExecutorSettings(
                        worker_id=runtime_settings.worker_id,
                        lease_seconds=runtime_settings.lease_seconds,
                        heartbeat_seconds=runtime_settings.heartbeat_seconds,
                        stage_timeout_seconds=runtime_settings.stage_timeout_seconds,
                    ),
                    telemetry=telemetry,
                )
                worker = Worker(
                    receiver=ServiceBusMessageReceiver(
                        sdk_receiver,
                        renewer=renewer,
                        max_lock_renewal_seconds=settings.bus.max_lock_renewal_seconds,
                    ),
                    executor=executor,
                    telemetry=telemetry,
                    max_wait_seconds=settings.bus.max_wait_seconds,
                    concurrency=runtime_settings.concurrency,
                )
                stop = asyncio.Event()
                install_stop_signals(stop)
                summary = await worker.run(
                    stop=stop, max_messages=args.max_messages, exit_when_idle=args.exit_when_idle
                )
        finally:
            await renewer.close()
    _print({"pool": pool.value, **asdict(summary)})
    return 0


async def _job(settings: PipelineSettings, telemetry: Telemetry, args: argparse.Namespace) -> int:
    async with azure_runtime(settings, telemetry, blob=False, bus=False) as runtime:
        job = await runtime.ledger.get(args.job_id)
        attempts = await runtime.ledger.attempts(args.job_id) if job else []
    _print(
        {
            "job": job.model_dump(mode="json") if job else None,
            "attempts": [attempt.model_dump(mode="json") for attempt in attempts],
        }
    )
    return 0 if job else 1


async def _replay(
    settings: PipelineSettings, telemetry: Telemetry, args: argparse.Namespace
) -> int:
    reason = args.reason.strip()
    if not reason:
        raise SystemExit("--reason must not be empty")
    async with azure_runtime(settings, telemetry, blob=False, bus=False) as runtime:
        job = await runtime.ledger.replay(
            args.job_id, actor=args.actor, reason=reason, now=runtime.services.clock.now()
        )
    _print({"job_id": job.job_id, "status": job.status.value, "actor": args.actor})
    return 0


async def _reconcile_legacy(
    settings: PipelineSettings, telemetry: Telemetry, args: argparse.Namespace
) -> int:
    async with azure_runtime(settings, telemetry, blob=False, bus=False) as runtime:
        catalog = LegacyCatalog(runtime.pool)
        if not await catalog.present():
            _print({"legacy_catalog": "absent", "applied": False})
            return 0
        items = await catalog.inventory(limit=args.limit)
        if args.apply:
            await catalog.record(items, now=runtime.services.clock.now())
    counts = Counter(item.state.value for item in items)
    _print({"inventoried": len(items), "states": dict(counts), "applied": bool(args.apply)})
    return 0


async def _status(
    settings: PipelineSettings, telemetry: Telemetry, args: argparse.Namespace
) -> int:
    async with azure_runtime(settings, telemetry, blob=False, bus=False) as runtime:
        report = await pipeline_status(runtime.pool, failures=args.failures)
    _print(report)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tizagent-pipeline", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser(
        "prepare-corpus", help="Offline, bounded corpus intake; never calls cloud services"
    )
    prepare.add_argument("--root", required=True)
    prepare.add_argument("--output", required=True, help="Staging directory outside the corpus")
    prepare.add_argument(
        "--office-image", help="Local image ID sha256:... or registry image pinned by @sha256"
    )
    prepare.add_argument("--max-total-gib", type=int, default=64)
    prepare.set_defaults(handler=_prepare_corpus)

    ingest = commands.add_parser(
        "ingest-corpus", help="Upload verified intake originals/derivatives and enqueue snapshots"
    )
    ingest.add_argument("--manifest", required=True)
    ingest.add_argument("--prefix", default="crv", help="Stable collection namespace")
    ingest.set_defaults(handler=_ingest_corpus)

    migrate = commands.add_parser("migrate", help="Apply packaged ledger migrations")
    migrate.add_argument("--dry-run", action="store_true", help="Apply then roll back")
    migrate.set_defaults(handler=_migrate)

    enqueue = commands.add_parser(
        "enqueue-snapshots", help="Plan snapshot jobs for blobs, pinned to their current ETag"
    )
    target = enqueue.add_mutually_exclusive_group(required=True)
    target.add_argument("--blob-name", action="append", help="Exact blob name (repeatable)")
    target.add_argument("--blob-list", help="UTF-8 file with one exact blob name per line")
    target.add_argument("--prefix", help="Blob name prefix to list")
    enqueue.add_argument("--limit", type=int, default=None, help="Stop after this many blobs")
    enqueue.add_argument("--priority", type=int, default=100)
    enqueue.set_defaults(handler=_enqueue_snapshots)

    arm = commands.add_parser(
        "enqueue-arm",
        help="Plan snapshotted blobs under a named routing policy (its own search release)",
    )
    arm.add_argument("--policy", required=True, help="Policy name in TIZ_POLICIES_PATH")
    arm_target = arm.add_mutually_exclusive_group(required=True)
    arm_target.add_argument("--blob-name", action="append", help="Exact blob name (repeatable)")
    arm_target.add_argument("--blob-list", help="UTF-8 file with one exact blob name per line")
    arm.set_defaults(handler=_enqueue_arm)

    cu_check = commands.add_parser(
        "cu-check", help="Verify the reviewed CU analyzers and model defaults (read-only)"
    )
    cu_check.set_defaults(handler=_cu_check)

    coordinator = commands.add_parser("coordinator", help="Run outbox dispatch and lease recovery")
    coordinator.set_defaults(handler=_coordinator)

    worker = commands.add_parser("worker", help="Consume one pool's queue")
    worker.add_argument("--pool", required=True, choices=[pool.value for pool in WorkerPool])
    worker.add_argument("--max-messages", type=int, default=None)
    worker.add_argument("--exit-when-idle", action="store_true")
    worker.set_defaults(handler=_worker)

    job = commands.add_parser("job", help="Show one job and its attempts")
    job.add_argument("--job-id", required=True)
    job.set_defaults(handler=_job)

    replay = commands.add_parser(
        "replay",
        help="Re-run a submission_unknown, needs_review or failed_permanent job (audited)",
    )
    replay.add_argument("--job-id", required=True)
    replay.add_argument("--reason", required=True, help="Why the replay is safe (audited)")
    replay.add_argument("--actor", required=True, help="Operator identity (audited)")
    replay.set_defaults(handler=_replay)

    legacy = commands.add_parser(
        "reconcile-legacy", help="Classify v1 catalog content against v2 evidence (read-only)"
    )
    legacy.add_argument("--apply", action="store_true", help="Record the classification")
    legacy.add_argument("--limit", type=int, default=100_000)
    legacy.set_defaults(handler=_reconcile_legacy)

    status = commands.add_parser("status", help="Read-only ledger, evidence and projection summary")
    status.add_argument("--failures", type=int, default=20, help="Recent problems to list")
    status.set_defaults(handler=_status)

    kg_import = commands.add_parser(
        "kg-import-registry", help="Record a Phase 1 registry run in the kg system of record"
    )
    kg_import.add_argument("--registry", required=True, help="registry.jsonl of a registry run")
    kg_import.set_defaults(handler=_kg_import)

    kg_aliases = commands.add_parser(
        "kg-import-aliases", help="Make a curated aliases.json the current kg alias set"
    )
    kg_aliases.add_argument("--aliases", required=True, help="aliases.json ({alias, tag} list)")
    kg_aliases.set_defaults(handler=_kg_aliases)

    kg_release = commands.add_parser(
        "kg-release", help="Freeze the current kg projection as a release (optionally load AGE)"
    )
    kg_release.add_argument(
        "--status",
        action="append",
        choices=sorted(STATUS_RANK),
        help=f"assertion statuses to project (default: {', '.join(DEFAULT_RELEASE_STATUSES)})",
    )
    kg_release.add_argument("--load-age", action="store_true", help="load the release into AGE")
    kg_release.add_argument("--activate", metavar="ENVIRONMENT", help="point ENVIRONMENT at it")
    kg_release.set_defaults(handler=_kg_release)

    kg_grant = commands.add_parser(
        "kg-grant-reader",
        help="Give an Entra service principal an execute-only kg read login",
    )
    kg_grant.add_argument("--login", required=True, help="Postgres role name (identity name)")
    kg_grant.add_argument("--object-id", required=True, help="Entra object (principal) ID")
    kg_grant.add_argument("--statement-timeout-ms", type=int, default=2000)
    kg_grant.set_defaults(handler=_kg_grant_reader)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = PipelineSettings()
    telemetry: Telemetry
    if args.handler is _prepare_corpus:
        configure_logging()
        telemetry = NoopTelemetry()
    else:
        telemetry = configure_telemetry(settings.telemetry)
    handler: Handler = args.handler
    return asyncio.run(handler(settings, telemetry, args))


if __name__ == "__main__":
    raise SystemExit(main())
