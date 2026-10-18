"""Apply the additive pilot prerequisites with workload identity and verified TLS."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "0002_knowledge_prerequisites"


async def apply(sql_path: Path, *, dry_run: bool) -> dict[str, object]:
    import asyncpg
    from azure.identity.aio import DefaultAzureCredential

    if os.environ.get("PG_DATABASE") != "ingestion-catalog":
        raise ValueError("PG_DATABASE must be ingestion-catalog")
    sql = sql_path.read_text(encoding="utf-8")
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    async with DefaultAzureCredential() as credential:
        token = await credential.get_token("https://ossrdbms-aad.database.windows.net/.default")
    connection = await asyncpg.connect(
        host=os.environ["PG_HOST"],
        user=os.environ["PG_USER"],
        password=token.token,
        database=os.environ["PG_DATABASE"],
        ssl=ssl.create_default_context(),
        timeout=30,
        command_timeout=120,
    )
    transaction = connection.transaction()
    try:
        await transaction.start()
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext($1))", VERSION)
        registry = await connection.fetchval("SELECT to_regclass('ingest.schema_migration')")
        prior_digest = None
        if registry is not None:
            prior_digest = await connection.fetchval(
                "SELECT sha256 FROM ingest.schema_migration WHERE version = $1", VERSION
            )
        if prior_digest is not None and prior_digest != digest:
            raise ValueError("Applied migration checksum differs; create a new migration version")
        if prior_digest is None:
            await connection.execute(sql)
            await connection.execute(
                "INSERT INTO ingest.schema_migration (version, sha256) VALUES ($1, $2)",
                VERSION,
                digest,
            )
        await connection.execute("SET LOCAL search_path = ag_catalog, pg_catalog")
        age_version = await connection.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'age'")
        node_count = await connection.fetchval(
            "SELECT nodes::text FROM ag_catalog.cypher('tizert_pilot', "
            "$$ MATCH (node) RETURN count(node) $$) AS (nodes ag_catalog.agtype)"
        )
        schemas = await connection.fetch(
            "SELECT nspname FROM pg_namespace WHERE nspname = ANY($1::text[]) ORDER BY nspname",
            ["ingest", "kg", "acl"],
        )
        result = {
            "version": VERSION,
            "sha256": digest,
            "dry_run": dry_run,
            "age_version": age_version,
            "pilot_nodes": node_count,
            "schemas": [row["nspname"] for row in schemas],
            "already_applied": prior_digest is not None,
        }
        if dry_run:
            await transaction.rollback()
        else:
            await transaction.commit()
        return result
    except BaseException:
        await transaction.rollback()
        raise
    finally:
        await connection.close()


async def verify_services() -> dict[str, object]:
    from azure.identity.aio import DefaultAzureCredential

    async with DefaultAzureCredential() as credential:
        cu_token = await credential.get_token("https://cognitiveservices.azure.com/.default")
        search_token = await credential.get_token("https://search.azure.com/.default")
    cu_endpoint = os.environ["CU_ENDPOINT"].rstrip("/")
    search_endpoint = os.environ["SEARCH_ENDPOINT"].rstrip("/")
    search_index = os.environ["SEARCH_INDEX"]
    results: dict[str, object] = {}
    for label, url, token in [
        ("cu_defaults", f"{cu_endpoint}/contentunderstanding/defaults?api-version=2025-11-01", cu_token.token),
        ("search_stats", f"{search_endpoint}/indexes/{search_index}/stats?api-version=2024-07-01", search_token.token),
    ]:
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.load(response)
        if label == "search_stats":
            if body["documentCount"] != 0:
                raise ValueError("Pilot index is not empty")
            results[label] = {"document_count": body["documentCount"]}
        else:
            mappings = body["modelDeployments"]
            if mappings.get("gpt-5.4-mini") != "crv-gpt-5.4-mini":
                raise ValueError("Unexpected CU completion mapping")
            if mappings.get("text-embedding-3-large") != "crv-embedding-3-large":
                raise ValueError("Unexpected CU embedding mapping")
            results[label] = "verified"
    fuseki_endpoint = os.environ["FUSEKI_ENDPOINT"].rstrip("/")
    query = urllib.parse.urlencode({"query": "SELECT (COUNT(*) AS ?count) WHERE { ?subject ?predicate ?object }"})
    request = urllib.request.Request(
        f"{fuseki_endpoint}/governance/sparql?{query}",
        headers={"Accept": "application/sparql-results+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.load(response)
    triple_count = int(body["results"]["bindings"][0]["count"]["value"])
    if triple_count != 0:
        raise ValueError("Pilot RDF dataset is not empty")
    results["fuseki_triples"] = triple_count
    for label, path, data, content_type in [
        ("fuseki_update", "/governance/update", b"INSERT DATA {}", "application/sparql-update"),
        ("fuseki_admin", "/$/datasets", None, "application/json"),
    ]:
        request = urllib.request.Request(f"{fuseki_endpoint}{path}", data=data, headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=30):
                raise ValueError(f"Unexpected exposed endpoint: {label}")
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403, 404, 405):
                raise
            results[label] = {"denied_status": error.code}
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sql", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify-services", action="store_true")
    arguments = parser.parse_args()
    result = asyncio.run(apply(arguments.sql, dry_run=arguments.dry_run))
    if arguments.verify_services:
        result["services"] = asyncio.run(verify_services())
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()