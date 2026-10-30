"""Keep a current Entra access token for Azure Database for PostgreSQL in a libpq password file.

Native sidecar of the db-export Job: libpq reads PGPASSFILE at every connection, so each psql and
pg_dump call of the export container authenticates with a valid token however long it runs. Also
copies the system CA bundle for PGSSLMODE=verify-full (the client image may not ship one).
Not named token.py: the script directory comes first on sys.path and would shadow the stdlib
``token`` module.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import time
from pathlib import Path

from azure.identity import WorkloadIdentityCredential

SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
CHECK_SECONDS = 60


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")


def _write(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


def main() -> None:
    root = Path(os.environ.get("PG_AUTH_DIR", "/run/pg"))
    host = os.environ["PGHOST"]
    user = os.environ["PGUSER"]
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    shutil.copyfile(CA_BUNDLE, root / "ca.crt")
    credential = WorkloadIdentityCredential()
    current = ""
    while True:
        # Cached until shortly before expiry, then refreshed by azure-identity.
        token = credential.get_token(SCOPE)
        if token.token != current:
            line = f"{_escape(host)}:5432:*:{_escape(user)}:{_escape(token.token)}\n"
            _write(root / "pgpass", line)
            current = token.token
            print(f"token written; expires_on={token.expires_on}", flush=True)
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
