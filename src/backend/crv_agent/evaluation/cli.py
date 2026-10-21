import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import ChatOpenAI

from crv_agent.adapters.models import reject_unapproved_tracing
from crv_agent.evaluation.dataset import load_questions
from crv_agent.evaluation.judge import LangChainJudge
from crv_agent.evaluation.runner import evaluate


def main() -> None:
    reject_unapproved_tracing()
    parser = argparse.ArgumentParser(
        description="Evaluate CRV verified answers (calls live backend)",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["dev", "heldout"], default="dev")
    parser.add_argument("--allow-heldout", action="store_true")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--question-id", action="append", default=[])
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args()
    if not args.confirm_live:
        parser.error("--confirm-live is required: this invokes the backend's model deployments")
    if args.split == "heldout" and not args.allow_heldout:
        parser.error("--allow-heldout is required to evaluate the held-out split")
    parsed = urlparse(args.base_url)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Remote evaluation requires HTTPS")
    questions = load_questions(args.dataset, args.split)
    if args.question_id:
        questions = [q for q in questions if q.id in args.question_id]
    if not questions:
        parser.error("The selected split has no questions")

    async def run() -> dict[str, object]:
        headers = {}
        if token := os.environ.get("CRV_EVAL_BEARER_TOKEN"):
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            base_url=args.base_url, headers=headers, timeout=150, follow_redirects=False,
        ) as client:
            if not args.judge:
                return await evaluate(client, questions)
            endpoint = os.environ["CRV_OPENAI_ENDPOINT"].rstrip("/")
            if not endpoint.endswith("/openai/v1"):
                endpoint += "/openai/v1"
            async with DefaultAzureCredential() as credential:
                model = ChatOpenAI(
                    base_url=endpoint + "/", model=os.environ["CRV_VERIFIER_DEPLOYMENT"],
                    api_key=get_bearer_token_provider(
                        credential, "https://cognitiveservices.azure.com/.default",
                    ), max_retries=0, timeout=30, max_completion_tokens=2048,
                )
                try:
                    return await evaluate(client, questions, LangChainJudge(model))
                finally:
                    await model.root_async_client.close()
                    model.root_client.close()
    report = asyncio.run(run())
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"questions": report["questions"], "report": str(args.output)}))
    if args.gate and not report["gates_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
