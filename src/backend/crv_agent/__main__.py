import argparse
import asyncio
import sys

import uvicorn

from crv_agent.runtime.bootstrap import application
from crv_agent.runtime.migrations import setup_memory
from crv_agent.runtime.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="CRV research backend (development only)")
    parser.add_argument("command", choices=["serve", "setup-memory"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "setup-memory":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(setup_memory(settings))
    else:
        if settings.local_owner and args.host not in {"127.0.0.1", "::1", "localhost"}:
            parser.error("Local identity may only bind to loopback")
        uvicorn.run(
            application(settings), host=args.host, port=args.port, log_level="info",
            loop="asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto",
        )


if __name__ == "__main__":
    main()
