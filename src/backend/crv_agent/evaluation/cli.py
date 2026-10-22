import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import ChatOpenAI

from crv_agent.adapters.models import reject_unapproved_tracing
from crv_agent.evaluation.dataset import load_questions
from crv_agent.evaluation.judge import LangChainJudge
from crv_agent.evaluation.runner import evaluate

ANSWERS_BEGIN, ANSWERS_END = "=== AGENT ANSWERS BEGIN ===", "=== AGENT ANSWERS END ==="
REPORT_BEGIN, REPORT_END = "=== AGENT EVAL REPORT BEGIN ===", "=== AGENT EVAL REPORT END ==="


async def wait_ready(client: httpx.AsyncClient, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            if (await client.get("/health/ready")).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"Backend not ready after {seconds:.0f} s")
        await asyncio.sleep(2)


def main(argv: list[str] | None = None) -> None:
    reject_unapproved_tracing()
    parser = argparse.ArgumentParser(
        description="Evaluate CRV verified answers (calls live backend)",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--answers-out", type=Path,
                        help="JSONL answers in the shared golden `score` context format")
    parser.add_argument("--print-answers", action="store_true",
                        help="Also print answers (no source text) between markers")
    parser.add_argument("--split", choices=["dev", "heldout"], default="dev")
    parser.add_argument("--allow-heldout", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--question-id", action="append", default=[])
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--wait-ready", type=float, default=0,
                        help="Seconds to wait for /health/ready before starting")
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_live:
        parser.error("--confirm-live is required: this invokes the backend's model deployments")
    if args.split == "heldout" and not args.allow_heldout:
        parser.error("--allow-heldout is required to evaluate the held-out split")
    if not 1 <= args.concurrency <= 8:
        parser.error("--concurrency must be between 1 and 8")
    parsed = urlparse(args.base_url)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Remote evaluation requires HTTPS")
    questions = load_questions(args.dataset, args.split)
    if args.question_id:
        questions = [q for q in questions if q.id in args.question_id]
    if not questions:
        parser.error("The selected split has no questions")

    answers = args.answers_out.open("w", encoding="utf-8") if args.answers_out else None

    def emit(record: dict[str, Any]) -> None:
        if answers is not None:
            answers.write(json.dumps(record, ensure_ascii=False) + "\n")
            answers.flush()

    async def run() -> dict[str, object]:
        headers = {}
        if token := os.environ.get("CRV_EVAL_BEARER_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            base_url=args.base_url, headers=headers, timeout=300, follow_redirects=False,
        ) as client:
            if args.wait_ready:
                await wait_ready(client, args.wait_ready)
            if not args.judge:
                return await evaluate(
                    client, questions, concurrency=args.concurrency, emit=emit,
                )
            endpoint = os.environ["CRV_OPENAI_ENDPOINT"].rstrip("/")
            if not endpoint.endswith("/openai/v1"):
                endpoint += "/openai/v1"
            async with DefaultAzureCredential() as credential:
                model = ChatOpenAI(
                    base_url=endpoint + "/", model=os.environ.get(
                        "CRV_EVAL_JUDGE_DEPLOYMENT", os.environ["CRV_VERIFIER_DEPLOYMENT"],
                    ), api_key=get_bearer_token_provider(
                        credential, "https://cognitiveservices.azure.com/.default",
                    ), max_retries=3, timeout=60, max_completion_tokens=2048,
                )
                try:
                    return await evaluate(
                        client, questions, LangChainJudge(model),
                        concurrency=args.concurrency, emit=emit,
                    )
                finally:
                    # An async token provider yields only an async client.
                    if model.root_async_client is not None:
                        await model.root_async_client.close()

    try:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            report = runner.run(run())
    finally:
        if answers is not None:
            answers.close()
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {key: value for key, value in report.items() if key != "records"}
    print(REPORT_BEGIN)
    print(json.dumps(summary, ensure_ascii=False))
    print(REPORT_END)
    if args.print_answers and args.answers_out:
        print(ANSWERS_BEGIN)
        sys.stdout.write(args.answers_out.read_text(encoding="utf-8"))
        print(ANSWERS_END)
    sys.stdout.flush()
    if args.gate and not report["gates_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
