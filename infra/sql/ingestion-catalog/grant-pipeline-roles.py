"""Map pipeline workload identities to PostgreSQL and grant the runtime role (Entra admin only).

Runs in-cluster as an identity that is a PostgreSQL Entra administrator. Dry run by default:
reports missing principals and grants; ``--apply`` creates them. Idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import ssl
from typing import Any

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,62}$")
SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


async def _connect(database: str, credential: Any) -> Any:
    import asyncpg

    token = await credential.get_token(SCOPE)
    return await asyncpg.connect(
        host=os.environ["PG_HOST"],
        user=os.environ["PG_USER"],
        password=token.token,
        database=database,
        ssl=ssl.create_default_context(),
    )


async def run(principals: list[str], role: str, database: str, *, apply: bool) -> dict[str, Any]:
    from azure.identity.aio import DefaultAzureCredential

    for name in [*principals, role]:
        if not NAME.match(name.replace("_", "-")):
            raise SystemExit(f"Refusing unexpected identifier {name!r}")
    report: dict[str, Any] = {"apply": apply, "created": [], "granted": []}
    async with DefaultAzureCredential() as credential:
        admin = await _connect("postgres", credential)
        try:
            existing = {
                row["rolname"]
                for row in await admin.fetch(
                    "SELECT rolname FROM pg_roles WHERE rolname = ANY($1::text[])", principals
                )
            }
            missing = [name for name in principals if name not in existing]
            report["missing_principals"] = missing
            if apply:
                for name in missing:
                    await admin.fetch(
                        "SELECT * FROM pgaadauth_create_principal($1, false, false)", name
                    )
                    report["created"].append(name)
        finally:
            await admin.close()
        if missing and not apply:
            report["missing_grants"] = principals
            return report
        catalog = await _connect(database, credential)
        try:
            if await catalog.fetchval("SELECT to_regrole($1)", role) is None:
                if apply:
                    raise SystemExit(f"Role {role} does not exist; apply migration 0003 first")
                report["role_missing"] = role
                report["missing_grants"] = principals
                return report
            pending = [
                name
                for name in principals
                if not await catalog.fetchval("SELECT pg_has_role($1, $2, 'MEMBER')", name, role)
            ]
            report["missing_grants"] = pending
            if apply:
                async with catalog.transaction():
                    for name in pending:
                        await catalog.execute(f'GRANT CONNECT ON DATABASE "{database}" TO "{name}"')
                        await catalog.execute(f'GRANT {role} TO "{name}"')
                        report["granted"].append(name)
        finally:
            await catalog.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--principal", action="append", required=True)
    parser.add_argument("--role", default="tizert_ingest_worker")
    parser.add_argument("--database", default="ingestion-catalog")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(args.principal, args.role, args.database, apply=args.apply))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
