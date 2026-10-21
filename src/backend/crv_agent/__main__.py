import argparse
import asyncio
import json
import sys

import uvicorn

from crv_agent.runtime.bootstrap import application
from crv_agent.runtime.migrations import grant_memory, setup_memory
from crv_agent.runtime.operations import run_checks
from crv_agent.runtime.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="CRV research backend (development only)")
    parser.add_argument(
        "command", choices=["serve", "setup-memory", "grant-memory-runtime", "check"],
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--login", help="Runtime PostgreSQL login for grant-memory-runtime")
    parser.add_argument("--models", action="store_true", help="check: ping each deployment")
    args = parser.parse_args()
    settings = Settings()
    if args.command == "serve":
        if settings.local_owner and args.host not in {"127.0.0.1", "::1", "localhost"}:
            parser.error("Local identity may only bind to loopback")
        uvicorn.run(
            application(settings), host=args.host, port=args.port, log_level="info",
            loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto",
        )
        return
    if args.command == "grant-memory-runtime" and not args.login:
        parser.error("grant-memory-runtime requires --login")
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        if args.command == "setup-memory":
            runner.run(setup_memory(settings))
            print(json.dumps({"setup_memory": "completed"}))
        elif args.command == "grant-memory-runtime":
            print(json.dumps(runner.run(grant_memory(settings, args.login))))
        else:
            report = runner.run(run_checks(settings, models=args.models))
            print(json.dumps(report, default=str))
            if report["failures"]:
                raise SystemExit(1)


if __name__ == "__main__":
    main()
