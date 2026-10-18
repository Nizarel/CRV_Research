"""Bake-off command line: ``sample`` and ``render`` run locally, ``score`` in the cluster."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from ...config import PipelineSettings, load_policies, require
from .model import GoldItem, Report, Sample
from .report import render_markdown
from .sample import blob_list, build_sample
from .score import score_arm

REPORT_BEGIN = "=== BAKEOFF REPORT BEGIN ==="
REPORT_END = "=== BAKEOFF REPORT END ==="


def load_gold(text: str, sample: Sample) -> list[GoldItem]:
    items = [GoldItem.model_validate_json(line) for line in text.splitlines() if line.strip()]
    ids = [item.id for item in items]
    if len(set(ids)) != len(ids):
        raise SystemExit("Gold item IDs must be unique")
    known = {document.id: document for document in sample.documents}
    for item in items:
        document = known.get(item.doc)
        if document is None:
            raise SystemExit(f"{item.id}: document {item.doc} is not in the sample")
        if item.page > document.page_count:
            raise SystemExit(f"{item.id}: page {item.page} is beyond {document.page_count}")
    return items


def _write(path: str, text: str) -> None:
    Path(path).write_bytes(text.encode("utf-8"))


def _sample(args: argparse.Namespace) -> int:
    review = json.loads(Path(args.review).read_text(encoding="utf-8"))
    sample = build_sample(
        review,
        load_policies(Path(args.policies)),
        caps={"cu_pages": args.cu_pages, "tokens": args.tokens},
        max_document_pages=args.max_document_pages,
    )
    _write(args.out, sample.model_dump_json(by_alias=True, indent=2) + "\n")
    _write(args.blobs, blob_list(sample))
    print(json.dumps({"documents": len(sample.documents), **sample.estimate}))
    return 0


async def _collect_and_score(args: argparse.Namespace) -> Report:
    from ...adapters.telemetry.otel import configure_telemetry
    from ...runtime.bootstrap import azure_runtime
    from .collect import collect_documents, collect_usage

    settings = PipelineSettings()
    telemetry = configure_telemetry(settings.telemetry)
    sample_text = Path(args.sample).read_text(encoding="utf-8")
    gold_text = Path(args.gold).read_text(encoding="utf-8")
    sample = Sample.model_validate_json(sample_text)
    gold = load_gold(gold_text, sample)
    policies = load_policies(require(settings.policies_path, "TIZ_POLICIES_PATH"))
    scores = []
    async with azure_runtime(settings, telemetry, bus=False) as runtime:
        container = require(runtime.source_container, "TIZ_BLOB__SOURCE_CONTAINER")
        for arm in args.arm or sample.arms:
            documents = await collect_documents(
                runtime.services,
                sample,
                container_url=container.url,
                policy_version=policies.get(arm).policy.version,
            )
            plans = sorted({doc.plan_id for doc in documents.values() if doc.plan_id})
            usage, jobs = await collect_usage(runtime.pool, plans)
            scores.append(score_arm(arm, gold, documents, sample, usage, jobs))
    return Report(
        generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
        sample_sha256=hashlib.sha256(sample_text.encode()).hexdigest(),
        gold_sha256=hashlib.sha256(gold_text.encode()).hexdigest(),
        gold_items=len(gold),
        arms=tuple(scores),
    )


def _score(args: argparse.Namespace) -> int:
    report = asyncio.run(_collect_and_score(args))
    # Delimited and indented: pod logs interleave JSON log records and limit line length.
    sys.stdout.write(f"{REPORT_BEGIN}\n{report.model_dump_json(by_alias=True, indent=1)}\n")
    sys.stdout.write(f"{REPORT_END}\n")
    sys.stdout.flush()
    return 0


def extract_report(text: str) -> Report:
    """The report from captured pod logs (delimited) or from a plain report file."""

    if REPORT_BEGIN in text:
        text = text.split(REPORT_BEGIN, 1)[1].split(REPORT_END, 1)[0]
    start = text.find("{")
    if start < 0:
        raise SystemExit("No report JSON found")
    return Report.model_validate_json(text[start:].strip())


def _render(args: argparse.Namespace) -> int:
    report = extract_report(Path(args.report).read_text(encoding="utf-8"))
    if args.json_out:
        _write(args.json_out, report.model_dump_json(by_alias=True, indent=2) + "\n")
    _write(args.out, render_markdown(report))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bakeoff", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    sample = commands.add_parser("sample", help="Build the sample and its blob list")
    sample.add_argument("--review", required=True, help="09-source-review-evidence.json")
    sample.add_argument("--policies", required=True, help="Routing-policy registry JSON")
    sample.add_argument("--out", required=True)
    sample.add_argument("--blobs", required=True)
    sample.add_argument("--cu-pages", type=int, default=600)
    sample.add_argument("--tokens", type=int, default=6_000_000)
    sample.add_argument("--max-document-pages", type=int, default=100)
    sample.set_defaults(handler=_sample)

    score = commands.add_parser("score", help="Collect arm evidence and score it (cluster)")
    score.add_argument("--sample", required=True)
    score.add_argument("--gold", required=True)
    score.add_argument("--arm", action="append", help="Arm to score (default: all)")
    score.set_defaults(handler=_score)

    render = commands.add_parser("render", help="Markdown from a score JSON or captured logs")
    render.add_argument("--report", required=True)
    render.add_argument("--out", required=True)
    render.add_argument("--json-out", help="Also write the extracted report JSON here")
    render.set_defaults(handler=_render)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
