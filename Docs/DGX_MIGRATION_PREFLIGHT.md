---
title: DGX Spark Data Migration Preflight
description: Readiness gates, verification commands, staging restore procedure, and sign-off requirements for moving CRV retrieval data from Azure PostgreSQL to DGX Spark.
author: CRV Research
ms.date: 2026-10-28
ms.topic: how-to
keywords:
  - dgx spark
  - postgresql
  - pgvector
  - migration
  - preflight
estimated_reading_time: 30
---

## Decision and scope

The source data has passed earlier copy and index checks. The DGX destination has not been
verified. Authorize a staging transfer only after gates P0 through P5 pass. A successful restore
does not authorize a production switch: gates P6 through P9 must also pass.

The initial move preserves PostgreSQL retrieval data, TE3 embeddings, provenance, and citation
identifiers. Keep Azure AI Search serving production. Keep ingestion and Azure OpenAI in Azure.
Do not rerun Document Intelligence, Content Understanding, or corpus embedding generation.

Use the [retrieval migration plan](./PGVECTOR_RETRIEVAL_TEST_PLAN.md) for the original stages and
quality thresholds. Its status and migration list predate subsequent work. Capture the actual
source state rather than treating that plan as an applied-migration inventory.

> [!IMPORTANT]
> On 2026-10-27 and 2026-10-28 the migration tooling was built and rehearsed, and the full
> `ingestion-catalog` export `f1028064148` went through the private Blob container onto the
> encrypted volume on dgx-niz01. It was restored into the isolated PostgreSQL 16 staging server,
> matched the source manifest exactly, and passed the scripted P6 checks. See
> [Implementation status on 2026-10-28](#implementation-status-on-2026-10-28). Gates stay NOT
> CHECKED until their owners sign off. Historical results are evidence to retain, not a
> substitute for validating the chosen export snapshot.

### Two authorization points

| Decision | Required evidence | What it permits |
|----------|-------------------|-----------------|
| Staging transfer | P0-P5 PASS, named owners, approved destination and data handling | Export, encrypted transfer, and isolated restore rehearsal |
| Production retrieval switch | P6-P9 PASS, approved exceptions, tested rollback, scheduled change | Route the agreed retrieval workload to DGX |

An unresolved requirement is NOT CHECKED or BLOCKED, never an assumed PASS. An N/A decision
needs a reason and an approver. Any exception that weakens a gate must be explicitly approved.

### Historical baseline from completed work

These observations come from earlier migration and evaluation reports discussed on October 27,
2026. Preserve their original artifacts in the evidence bundle before relying on them for sign-off.

| Observation | Earlier result | Limitation |
|-------------|----------------|------------|
| Source copy | 145,855 chunks from 1,929 documents | Recount the selected release and verify freshness |
| Copy parity | Zero missing, mismatched, extra, or vectorless rows | Revalidate against the actual export snapshot |
| TE3 HNSW index | Built successfully | Check extension compatibility and validity after restore |
| ANN recall@50 | 1.0 on 10 sampled queries; minimum gate 0.95 | Small sample, not the complete golden-question gate |
| Candidate retrieval CEIL | PostgreSQL minus Azure anchor recall +0.011 | Preserve the run configuration and input revision |
| NVIDIA 1B reranker | Offline dev53 anchor@10 0.585 versus Azure 0.550 | Hosted API timing and quality do not prove local GB10 support |
| Production routing | Azure AI Search at the last recorded check | Confirm current routing before migration |

The reranker result is not the `A1` judge@10 gate. It also does not prove that the reranker is
integrated into the backend or that an end-to-end answer evaluation has passed. Treat newer
in-progress backend work and evaluation jobs as pending until their final reports are recorded.

## Implementation status on 2026-10-28

### Decisions

* Target PostgreSQL 16 for the first move; the source runs 16.15. PostgreSQL 18 with AGE 1.8 is
  deferred as the riskier option.
* Match the source extension versions exactly: Apache AGE 1.6.0, pgvector 0.8.2, pg_trgm 1.6, and
  unaccent 1.1.
* Restore onto dgx-niz01 only. dgx-niz02 is not used in this milestone; its cached
  `crv-postgres:pg18-age1.8.0-pgvector0.8.7` image has unknown provenance and is not used.
* Export from AKS. Without a site-to-site VPN, AKS cannot reach the DGX network, so the bundle
  waits in the private Blob container `crv-migration`. dgx-niz01 fetches it from there through an
  SSH reverse tunnel over the operator's point-to-site VPN.
* Keep owners and grants. The dump is taken without `--no-owner` or `--no-acl`. The target first
  recreates every referenced role as a NOLOGIN placeholder, so ownership, grants,
  security-definer functions, and default privileges come back exactly as on Azure.
* Freeze the whole database, not one release, for a full export, and prove the freeze with write
  counters (see P5).
* Scope is `ingestion-catalog` only. Conversation state in `crv-agent-memory` is not moved
  (decided 2026-10-28). `crv-research-web` keeps running during the export: its identity reads
  `ingestion-catalog` with read-only transactions by default, and the freeze check proves no writes.
* Keep everything on dgx-niz01 that holds CRV data (database files, bundles, restore evidence, and
  the staging password) on a LUKS2-encrypted volume (decided 2026-10-28). The operator types the
  passphrase after every reboot; it is never stored on the host.
* After the restore, the target is one edge deployment over dgx-niz01 and dgx-niz02 that must
  eventually run without Azure, with no Azure fallback in this phase (decided 2026-10-28). The
  edge re-embeds the corpus with BGE-M3 locally; TE3 stays only as the parity baseline.
  Generation starts on NVIDIA API catalog models (phase 1), so prompts with CRV excerpts reach
  the NVIDIA API until local serving replaces it (phase 2). The link between the two hosts is not
  encrypted. Source PDFs come from the operator laptop with the same folder structure (all
  decided 2026-10-28). See [placement](./DGX_EDGE_PLACEMENT.md), [runbook](./DGX_EDGE_RUNBOOK.md),
  [validation](./DGX_EDGE_VALIDATION.md) and [identity](./DGX_EDGE_IDENTITY.md).

### Tooling

| Path | Purpose |
|------|---------|
| [inventory.sql](../infra/sql/migration/inventory.sql) | Read-only JSON inventory: settings, database, extensions, schemas, relations, referenced roles and memberships, unreadable objects, AGE graphs, migration ledger, release status, sessions |
| [freeze-check.sql](../infra/sql/migration/freeze-check.sql) | Write counters of every table and every sequence position |
| [manifest.sql](../infra/sql/migration/manifest.sql) | OID-free definitions, owners, ACLs, and per-table row digests for the source/target comparison |
| [chunks.sql](../infra/sql/migration/chunks.sql) | Projection IDs, payload digests, and vector fingerprints of every chunk |
| [prepare-target.sql](../infra/sql/migration/prepare-target.sql) | Placeholder roles, memberships, database, database grants, and settings |
| [age-graphid-repair.sql](../infra/sql/migration/age-graphid-repair.sql) | Resets AGE graph IDs to their namespace OIDs after a restore |
| [db-export](../infra/kubernetes/pipeline/db-export/) and `render-pilot.ps1 -Job db-export` | AKS export job: token sidecar, export, checksums, Blob upload |
| [postgres16/Dockerfile](../infra/dgx/postgres16/Dockerfile) | PostgreSQL 16.15 with AGE 1.6.0 and pgvector 0.8.2, native ARM64 |
| [compose.yaml](../infra/dgx/staging/compose.yaml) | Staging server on the DGX loopback; data, bundles, and password on the encrypted volume; refuses to start while it is locked |
| [luks-volume.sh](../infra/dgx/staging/luks-volume.sh) | Creates, opens, and closes the LUKS2 volume at `/srv/crv-secure` (run with `sudo`) |
| [fetch-bundle.sh](../infra/dgx/staging/fetch-bundle.sh) | Parallel ranged download of a bundle onto the encrypted volume, verified against `SHA256SUMS` before it is moved into place |
| [restore.sh](../infra/dgx/staging/restore.sh) | Fail-closed restore and comparison |
| [verify-staging.sh](../infra/dgx/staging/verify-staging.sh) | P6 checks: counts, vector dimensions, indexes, reader privileges, Cypher on every graph, `retrieval-verify` |
| [synthetic-rehearsal.sh](../infra/dgx/staging/synthetic-rehearsal.sh) | End-to-end rehearsal with synthetic data only, including the M3 backfill, index and search with a stub embedder |
| [edge-logins.sql](../infra/sql/migration/edge-logins.sql) and [edge-logins.sh](../infra/dgx/edge/edge-logins.sh) | Edge backend and migrator logins with password files on the volume, and the empty `crv-agent-memory` database |
| [edge compose.yaml](../infra/dgx/edge/compose.yaml) and [edge.env.example](../infra/dgx/edge/edge.env.example) | Edge stack on dgx-niz01: PostgreSQL, backend and web, no Azure settings |
| `retrieval-embed` and `retrieval-verify --model m3` | Pipeline commands: fill `embedding_m3` from a local OpenAI-compatible server; check M3 recall and plans |

### Evidence

| Check | Result |
|-------|--------|
| Source server | PostgreSQL 16.15, Burstable B2s, 120 IOPS, private access, Entra authentication only |
| Source extensions | `ingestion-catalog`: age 1.6.0, vector 0.8.2, pg_trgm 1.6, unaccent 1.1; no Azure-only extension installed |
| Source database | UTF8, libc `en_US.utf8`, 6.53 GB, owner `id-crv-aks-workload`, four `crv_research_*` AGE graphs, migrations `0002_crv_baseline` through `0027` |
| Source release | `crv-v1`: 145,855 chunks with 145,855 TE3 vectors and no M3 vectors |
| Source retrieval baseline, run `b1027214054` | Preflight, parity, and explain passed with zero mismatches; unscoped HNSW 9.6 s, cold lexical 169 s, scoped 115 ms |
| Inventory export `i1027224229` | Completed in 21 s; checksums verified after transfer to the DGX |
| Schema exports `s1027230647` and `s1027230710` | Restored into throwaway PostgreSQL 16 containers on dgx-niz01; manifests identical (928 and 33 lines) |
| Synthetic rehearsal `r20261028062237` | 26 migrations and 2,000 chunks; full manifest (501 lines) and chunk list identical; AGE repair fixed 2 stale graph IDs; `retrieval-verify` passed with recall@50 of 1.0 on 20 queries (p95 12.4 ms); the reader searches through the function but cannot read the table |
| Full export `f1028064148` (2026-10-28) | Write counters unchanged before, during, and after the dump; manifest 12 min, chunk list 46 s, `pg_dump` 2.75 GB (zstd) in 4 min, upload 32 s. Held in Blob `crv-migration/f1028064148/ingestion-catalog/` |
| Encrypted volume (2026-10-28) | LUKS2 image of 100 GB opened as `/dev/mapper/crv-secure`, ext4 at `/srv/crv-secure` with `nosuid,nodev,noexec`, 93 GB free; bundle, log, and secret directories mode 0700 |
| Transfer to dgx-niz01 (2026-10-28) | Laptop download with `az storage blob download-batch` fetched 102 MB in 23 min, then failed. Direct fetch by dgx-niz01 through the SSH reverse tunnel: 16 unbounded streams reached about 11 MB/s, then stalled the VPN; 4 streams of 2 MiB range requests sustained 5-6 MB/s and resumed after VPN reconnects and a dgx-niz01 reset. The first complete copy of the dump failed its checksum (see Findings); it was fetched again in 451 s (about 6.1 MB/s). All nine files passed `sha256sum --check --strict` at 17:23:52 UTC and moved to `/srv/crv-secure/bundles/f1028064148`; the corrupt copy was deleted after analysis |
| Staging restore of `f1028064148` (2026-10-28) | First attempt stopped at `chunk_te3_hnsw_idx`: the parallel HNSW build could not get its 4 GB shared memory segment under the 2 GB `shm_size` (see Findings). The single transaction rolled back, leaving only the empty database from `prepare-target.sql`. With `shm_size: 8g`, the rerun with `REPLACE_DATABASE=1` restored in 128 s; the AGE repair reset 4 of 4 graph IDs; restore, repair, `vacuumdb --analyze`, and comparison took 256 s. Manifest (1,268 lines, including a row digest of every table) and chunk list identical; database 6,355 MB; evidence under `/srv/crv-secure/restore-logs/ingestion-catalog-restore-20261028T173113Z` |
| P6 scripted checks (2026-10-28) | `verify-staging.sh`: release `crv-v1` with 145,855 chunks, all holding 3,072-dimension TE3 vectors; no invalid indexes; `crv_kg_reader` can use the schema and execute `search` but cannot read or write the table; no `PUBLIC` table grants. Cypher vertex and edge counts equal the label tables on all four graphs. `retrieval-verify` as a local login with the restored worker and reader roles: parity on 145,855 rows with zero mismatches; recall@50 of 1.0 on all 30 sampled queries (approximate p50 13.1 ms, p95 24.9 ms); plans use `chunk_te3_hnsw_idx` unscoped (5.6 ms), `chunk_release_content_idx` scoped (0.9 ms), and `chunk_terms_search_text_idx` lexical (24.6 ms), with no sequential scan. Timings are warm-cache, right after the restore |
| Compose staging test | Refused to start without the encrypted path; healthy, loopback-only, no restart policy; generated password accepted over SCRAM; schema restore identical (928 lines) |
| dgx-niz01 | Ubuntu 24.04.5 on aarch64, 121 GB RAM, 3.4 TB free, Docker 29.6.2, Compose 5.2.0, NTP synchronized |
| Images | `crv-postgres16:age1.6.0-pgvector0.8.2` (`sha256:cdeaeb0c...`), `crv-pipeline:4520b64-arm64` (`sha256:be407042...`), export client `migration/postgres@sha256:537d0998...` in ACR |

### Findings

* Azure grants `azure_pg_admin` membership in `pg_signal_autovacuum_worker`, a role PostgreSQL 16
  lacks. Placeholder roles receive no predefined-role memberships.
* The dump carries 75 Azure privilege changes on `pg_catalog` objects. The restore leaves them out.
* On Azure, `ag_catalog` is owned by `azuresu` and is not an extension member; the restore
  recreates it the same way.
* With AGE 1.6.0, Cypher reads still worked with the stale graph IDs. The repair restores the
  invariant that graph ID equals namespace OID, which new graphs and labels rely on; without it a
  later `create_graph` can collide with a stale ID.
* A NULL ACL and an explicit owner-only ACL grant the same privileges, and `pg_dump` restores the
  latter as the former, so the manifest prints a missing ACL as its default.
* Docker bridge-network builds on dgx-niz01 intermittently failed to reach PyPI; the pipeline
  image was built with `--network host`.
* The point-to-site VPN can drop without notice. The Blob name then resolves to the public
  address, and the storage firewall correctly answers 403 `AuthorizationFailure`. Pin the private
  endpoint address (`curl --resolve`), resume partial downloads, and verify every file against
  `SHA256SUMS`.
* The storage account cannot be opened to a public IP, even briefly. The `MCAPSGovDeployPolicies`
  assignment includes `StorageAccount_PublicNetwork_Modify`: an update to
  `publicNetworkAccess=Enabled` succeeds, but the policy keeps the account `Disabled` (verified on
  2026-10-28 with a single-address rule, which was removed again). The transfer needs the VPN.
  With it up, an SSH reverse tunnel from the laptop lets dgx-niz01 fetch the bundle itself in
  parallel ranges, with TLS end to end and no copy on the laptop.
* The point-to-site VPN carries traffic inside a single TCP connection. Sustained bulk streams
  (16 unbounded streams at about 11 MB/s, or 4 streams limited to 2 MiB/s) filled its queues
  within minutes: new connections through it then timed out and the client stopped passing
  traffic until it reconnected. Small range requests and resumable ranges keep the transfer
  moving; a fresh VPN session restores normal latency.
* dgx-niz01 reset without warning at 08:01 and 09:57 PDT on 2026-10-28. The journal stops
  mid-stream with no shutdown sequence, kernel error, or kdump, and the GPU was idle; the causes
  (power, firmware, hardware watchdog) are not known. Every restart locks the encrypted volume
  until an operator types the passphrase, which stops the staging server. Find the cause before
  P8 sign-off.
* A reset can lose the last writes of a file while its length survives. After the 09:57 reset,
  each of the four dump ranges kept its length but held one run of zero bytes (174 to 2,284
  bytes, ending on a 4 KiB page boundary) about 217 to 245 MiB in, shortly behind where each
  stream was writing (the last progress reading, 1,096 MB in total, was at 09:57:29). The
  fetch resumed from each range's length, skipped the lost bytes, and only `sha256sum --check`
  caught the damage. [fetch-bundle.sh](../infra/dgx/staging/fetch-bundle.sh) now drops the last
  64 MiB of every range written before the current boot and re-hashes a complete file before
  skipping it.
* A parallel pgvector HNSW build keeps its graph in one shared memory segment as large as
  `maintenance_work_mem`. With `maintenance_work_mem=4GB` and `shm_size: 2g`, the full restore
  failed at `chunk_te3_hnsw_idx` with `could not resize shared memory segment ... No space left on
  device`. The synthetic rehearsal (2,000 chunks, 1 GB shm, `maintenance_work_mem=1GB`) did not
  exercise this path. The staging and edge compose files now set `shm_size: 8g`; keep it above
  `maintenance_work_mem`.

### Open before a full export

* [x] Approve at-rest encryption: LUKS2 volume on dgx-niz01 (2026-10-28).
* [x] Decide on conversation data: `crv-agent-memory` is not moved (2026-10-28).
* [x] Keep the pipeline at zero replicas with KEDA paused through the export (verified 2026-10-28).
* [x] Create the encrypted volume on dgx-niz01 (`sudo`, operator passphrase; 2026-10-28).
* [ ] Assign gate owners and record the approvals in the sign-off record.

## Gate register

Assign an owner and evidence location to every row. Update status only after checking the
specific host, release, revision, and configuration selected for migration.

| Gate | Requirement | Initial status | Owner | Evidence |
|------|-------------|----------------|-------|----------|
| P0 | Scope, release, data classification, approvals, and source baseline | NOT CHECKED | TBD | TBD |
| P1 | DGX access, OS, memory, storage, and time synchronization | NOT CHECKED | TBD | TBD |
| P2 | ARM64 database image, PostgreSQL extensions, and locale compatibility | NOT CHECKED | TBD | TBD |
| P3 | Private routes, DNS, TLS, firewall policy, and artifact access | NOT CHECKED | TBD | TBD |
| P4 | Export identity, local roles, Azure identity, and secret handling | NOT CHECKED | TBD | TBD |
| P5 | Consistent export, transfer capacity, and recovery preparation | NOT CHECKED | TBD | TBD |
| P6 | Restored data, digests, vectors, indexes, graph, and permissions | NOT CHECKED | TBD | TBD |
| P7 | Backend portability, retrieval quality, answers, and performance | NOT CHECKED | TBD | TBD |
| P8 | Backup restore, monitoring, restart recovery, and failure handling | NOT CHECKED | TBD | TBD |
| P9 | Parallel validation, release synchronization, cutover, and rollback | NOT CHECKED | TBD | TBD |

## Command conventions and evidence

Linux `bash` examples run on the named DGX host or an approved Linux export runner.
PowerShell examples run from the repository root on the operator workstation. SQL examples
run through an approved database connection. Replace every `REPLACE_*` value before use.

Inventory commands are read-only unless their section explicitly says otherwise. Read-only
database checks can still create load; set timeouts and use an agreed source monitoring window.
Do not run full-table scans, ANN comparisons, or dumps concurrently without measuring impact.

Use an evidence directory outside Git with restricted access. Record UTC timestamps, operator,
hostname, Git revision, image digests, commands, exit codes, and redacted results. Logs, dumps,
manifests, and query excerpts may contain confidential CRV material.

```bash
export DGX_PRIMARY='REPLACE_PRIMARY_PRIVATE_FQDN'
export DGX_SECONDARY='REPLACE_SECONDARY_PRIVATE_FQDN'
export DGX_OPERATOR='REPLACE_OPERATOR_LOGIN'
export SOURCE_SERVICE='crv-azure-source'
export RESTORE_SERVICE='crv-dgx-restore-admin'
export TARGET_SERVICE='crv-dgx-staging-reader'
export RELEASE_ID='crv-v1'
```

These service names refer to operator-provisioned libpq service entries, not existing repository
configuration. Provision them through approved secret handling. Set the database name, host,
port, user, CA path, `sslmode=verify-full`, and `connect_timeout=10` explicitly. Keep credentials
out of command arguments, chat, shell history, manifests, and committed environment files.
Protect any password file with mode `0600`; use short-lived credentials where supported.

## P0 Scope and source inventory

### Confirm boundaries and ownership

* [ ] Name the migration lead, DGX administrator, database owner, security approver, and application owner.
* [ ] Approve the site, operators, storage location, retention, and CRV data classification.
* [ ] Record the selected release, ingestion cutoff, Git revision, and immutable pipeline and backend image digests.
* [ ] Decide whether the transfer is a one-time rehearsal or the start of ongoing release synchronization.
* [ ] Define source-load stop thresholds and the operator authorized to cancel the export.
* [ ] Confirm Azure AI Search remains the production default throughout staging.
* [ ] Decide which schemas are included and whether copying operational or conversation data is permitted.

The original Stage 2 plan restores the complete `ingestion-catalog` database. A full database
export may include catalog, graph, operational, and application data beyond the retrieval rows.
Review that scope before exporting. A retrieval-schema-only dump is not equivalent: it can omit
roles, extensions, catalog relationships, graph objects, and runtime dependencies.

### Capture current source facts

Run with the approved inventory identity. Use the actual connection rather than assuming the
server version, SKU, database size, or extension set from an earlier report.

```bash
psql "service=$SOURCE_SERVICE" -X -v ON_ERROR_STOP=1
```

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '30s';
SELECT current_database(), current_user, version();
SELECT pg_size_pretty(pg_database_size(current_database())) AS database_size;
SELECT extname, extversion FROM pg_extension ORDER BY extname;
SELECT datname, pg_encoding_to_char(encoding), datcollate, datctype
FROM pg_database
WHERE datname = current_database();
SELECT * FROM crv_retrieval.release_status('crv-v1');
SELECT schemaname, indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'crv_retrieval'
ORDER BY indexname;
COMMIT;
```

Change the SQL release literal if P0 selects a different release. Export the applied migration
ledger and its recorded checksums using the owning migration tooling. Compare it with the Git
revision being shipped. Retrieval SQL currently exists through `0027`; this is not proof that
every migration is applied. Never edit an applied migration to make a restore pass.

Record current production health, search backend selection, and active migration or evaluation
jobs. Coordinate with their owners before adding database load. Do not stop their jobs merely
to perform this preflight.

Pass P0 when scope and data handling are approved, baseline artifacts are retained, and the
selected release and source revision are unambiguous.

## P1 DGX access and host capacity

### Access and operating system

Run from the approved Linux operator machine after verifying SSH host fingerprints through an
independent channel. Never disable host-key checking to get a successful connection.

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 "$DGX_OPERATOR@$DGX_PRIMARY" \
  'hostname; uname -m; cat /etc/os-release; date -u; uptime'
ssh -o BatchMode=yes -o ConnectTimeout=10 "$DGX_OPERATOR@$DGX_SECONDARY" \
  'hostname; uname -m; cat /etc/os-release; date -u; uptime'
```

On each DGX host, capture:

```bash
lscpu
free -h
lsblk -o NAME,SIZE,FSTYPE,MOUNTPOINTS
df -hT
df -i
timedatectl status
docker version
docker compose version
nvidia-smi
```

Expect Linux ARM64 (`aarch64`) and the intended DGX OS release. Record actual RAM, storage,
GPU, driver, and container-toolkit versions. Two Sparks do not form one transparent memory
pool; the advertised 128 GB per machine is shared between CPU and GPU workloads.

Docker access is effectively privileged. Approve access and host hardening through the DGX
administrator. If elevated access is required, the administrator runs the command directly;
do not route an elevation password through chat or automation logs.

### Storage and memory budget

Measure the complete included database, largest table, indexes, and expected dump size. Historical
sizes from the small Azure server are not a capacity specification for the DGX installation.

| Allocation | Required planning input |
|------------|-------------------------|
| PostgreSQL data volume | Restored heap, TOAST, graph, and all indexes, plus approved growth |
| Export and incoming area | Compressed dump, encrypted copy if separate, checksums, and manifests |
| Restore and rebuild workspace | Temporary files, index builds, and `ANALYZE` headroom |
| WAL and replication | Measured restore WAL, retention, standby lag, and outage allowance |
| GPU model cache | Exact model artifacts, runtime layers, and download workspace |
| Backups | Approved retention and at least one independent recovery copy |
| Memory | OS, PostgreSQL, model weights, activations, request concurrency, and safety reserve |

As an initial planning allowance, reserve at least three times the measured database size for
database-related staging work, then add models, backups, WAL, and growth separately. Replace
this allowance with measured rehearsal peaks. Do not treat it as a guaranteed upper bound.

* [ ] Use durable local storage and a named persistent volume, not the container writable layer.
* [ ] Verify mount ownership, filesystem support, encryption at rest, and backup coverage.
* [ ] Reserve capacity for both the restored index and a replacement index during maintenance.
* [ ] Agree disk-free and memory stop thresholds; a proposed floor is 20% free disk.
* [ ] Measure resource contention with other DGX workloads before authorizing a restore.
* [ ] Do not run destructive disk benchmarks against a database or backup volume.

Pass P1 when both required hosts are accessible, time is synchronized, and the capacity budget
fits without relying on swap or unmeasured GPU memory sharing.

## P2 ARM64 and PostgreSQL compatibility

The staging image is [postgres16/Dockerfile](../infra/dgx/postgres16/Dockerfile): the official
`postgres:16-bookworm` image, pinned by its multi-architecture digest, with AGE and pgvector built
from checksum-verified source releases. The integration-test image
[postgres.Dockerfile](../src/ingestion/tests/integration/postgres.Dockerfile) pins a different AGE
image; do not use it for staging. Do not assume any other image includes an ARM64 manifest or
that its extension packages match the DGX target.

### Image inspection

Run from a machine with authorized registry access. Registry inspection is read-only; it still
needs network access and can require authentication.

```bash
export PG_IMAGE='REPLACE_REPOSITORY@sha256:REPLACE_DIGEST'
docker buildx imagetools inspect "$PG_IMAGE"
```

Record the `linux/arm64` manifest and selected digest. Build or pull only after the relevant
change and download approval. Verify the actual architecture inside the selected container.
An amd64 image running under emulation is not an accepted production substitute.

### Compatibility matrix

| Component | Requirement |
|-----------|-------------|
| PostgreSQL | Match the source major version for the first rehearsal; the original plan uses 16 |
| Dump tools | Prefer the source major version; `pg_dump` must not be older than the source server |
| Restore tools | Use a compatible `pg_restore`, preferably from the same toolset as `pg_dump` |
| pgvector | At least 0.8.0 and compatible with the source extension version and halfvec HNSW |
| Apache AGE | Match source extension version and PostgreSQL ABI; the test image uses AGE 1.6.0 |
| Other extensions | Install every extension required by the selected schemas, including `unaccent` and `pg_trgm` |
| Locale and collation | Match encoding and intended locale/provider behavior; retain French and English text-search support |
| Server settings | Review preload libraries, AGE loading, connection limits, timeouts, WAL, and memory |
| Persistence | Confirm data directory path, container UID/GID, mount permissions, and PostgreSQL checksums policy |

Use logical dump and restore across architectures. Do not copy Azure PostgreSQL data files or
assume an Azure physical backup can be attached to a DGX PostgreSQL container.

Check installed and available extension versions on the isolated target before loading data:

```sql
SELECT version();
SHOW shared_preload_libraries;
SELECT name, version, installed
FROM pg_available_extension_versions
WHERE name IN ('vector', 'age', 'unaccent', 'pg_trgm')
ORDER BY name, version;
```

The dump can contain `CREATE EXTENSION` entries. Installing extension binaries is different
from creating extension objects. If the DBA pre-creates extensions, review the restore table
of contents and handle those entries explicitly. Do not ignore duplicate-object errors.

Pass P2 only after a small disposable restore proves extension creation, AGE graph access,
French accent handling, TE3 vector dimensions, and halfvec index support on native ARM64.
Retain the image digest and test output. This disposable test writes only to an approved target.

## P3 Networking, DNS, and TLS

### Required paths

| Source | Destination | Typical port | Why it is needed |
|--------|-------------|--------------|------------------|
| Operator | Both DGX hosts | TCP 22 | Administration and approved transfer |
| Export runner | Azure PostgreSQL private FQDN | TCP 5432 | Read-only inventory and logical dump |
| DGX backend | Azure OpenAI endpoint | TCP 443 | Initial generation and TE3 query embeddings |
| DGX backend | CRV Blob endpoint | TCP 443 | Source documents and citation artifacts remain in Azure |
| DGX validation runner | Azure AI Search endpoint | TCP 443 | Side-by-side comparison and fallback validation |
| DGX services | Microsoft Entra endpoints | TCP 443 | Token acquisition and certificate-based identity |
| DGX hosts | Approved image and model registries | TCP 443 | Pinned runtime images and model artifacts |
| Spark 2 | Spark 1 PostgreSQL | TCP 5432 | Standby replication, if selected |
| DGX hosts | Site DNS and time services | Site-defined | Private resolution and clock synchronization |

The source PostgreSQL path need not originate from DGX if an approved runner inside Azure
exports and transfers the archive. Record which machine performs each check. Do not expose
PostgreSQL publicly or widen Azure firewalls to bypass a private connectivity failure.

On DGX, use the actual endpoint names captured from the deployed configuration. Existing
pipeline defaults are examples, not proof of the current deployment:

```bash
export AZURE_PG_HOST='psql-crv-awmc6icu2kueq-ncus.postgres.database.azure.com'
export AZURE_BLOB_HOST='stawmc6icu2kueq.blob.core.windows.net'
export AZURE_SEARCH_HOST='gptkb-awmc6icu2kueq.search.windows.net'
export AZURE_OPENAI_HOST='cog-awmc6icu2kueq.cognitiveservices.azure.com'
getent ahostsv4 "$AZURE_PG_HOST"
getent ahostsv4 "$AZURE_BLOB_HOST"
getent ahostsv4 "$AZURE_SEARCH_HOST"
getent ahostsv4 "$AZURE_OPENAI_HOST"
ip route get REPLACE_EXPECTED_PRIVATE_IP
```

Verify each private endpoint against the approved private IP and route, not merely an RFC1918
address. Test from the final application container as well as the host. A workstation VPN test
does not prove a site-to-site route or container DNS configuration.

### TLS and authenticated probes

```bash
openssl s_client -connect "$AZURE_BLOB_HOST:443" \
  -servername "$AZURE_BLOB_HOST" -verify_hostname "$AZURE_BLOB_HOST" \
  -verify_return_error </dev/null
curl --connect-timeout 10 --max-time 20 --silent --show-error \
  --output /dev/null --write-out '%{http_code}\n' "https://$AZURE_BLOB_HOST/"
```

A `401` or `403` can demonstrate HTTP reachability but is not an authorization PASS. Follow
with an approved authenticated read of a known CRV artifact from the application container.
Verify its identifier and checksum. Test Azure OpenAI and Search with their actual runtime
identities and approved requests; embedding and generation probes can incur charges.

* [ ] Verify certificate chains and hostnames without `curl -k` or disabled database TLS checks.
* [ ] Confirm private DNS forwarding and routes survive host and VPN restarts.
* [ ] Validate egress policy from the final container network, not an unrestricted diagnostic container.
* [ ] Confirm no PostgreSQL or model-server listener is published on an untrusted interface.
* [ ] Record firewall owners and a bounded procedure for approved changes.

On the Windows operator machine, Global Secure Access has previously overridden private-link
DNS despite an active VPN. Inspect effective DNS and routes before long ACR or transfer work.
Do not silently bypass GSA, edit hosts files, or change corporate security policy.

Pass P3 when every required runtime path passes DNS, route, TLS, and authenticated-access checks.

## P4 Identity, roles, and secrets

### Separate the identities

| Identity | Required boundary |
|----------|-------------------|
| Source export operator | Approved read access to all included objects; no schema or data changes |
| Local restore administrator | Temporary DBA capability on the isolated target only |
| Local object owner | Reviewed ownership for tables and security-definer functions |
| Backend retrieval reader | Execute-only retrieval interface; no direct evidence-table writes |
| Backend state writer | Only the required application-state schema and operations |
| Replication user | Only replication capability, if a standby is configured |
| Azure application identity | Only required Azure OpenAI, Blob, and evaluation Search permissions |

Never migrate Azure-managed platform roles, login passwords, or token material by blindly
replaying `pg_dumpall --globals-only`. Prepare a reviewed local role mapping instead. Catalog
and graph references to application roles still need explicit handling.

The existing
[PostgreSQL pool](../src/backend/crv_agent/adapters/postgres/memory.py)
obtains an Azure PostgreSQL Entra token and enforces TLS. The
[infrastructure factory](../src/backend/crv_agent/runtime/infrastructure.py)
uses that pool for PostgreSQL dependencies. A local password or client-certificate login must
be implemented and tested where needed; changing only the hostname or backend setting is not
proof of portability. Inventory retrieval, graph, memory, and checkpoint connections separately.

Off-Azure access to Azure services also needs a non-AKS identity path. The original plan proposes
a certificate-based service principal. Approve the exact identity, certificate storage,
permissions, token renewal, rotation, and revocation. AKS workload identity is not automatically
available on Docker Compose.

* [ ] Verify source dump access includes relevant tables, sequences, and AGE objects.
* [ ] Confirm no row-level security or missing grants silently omit intended export data.
* [ ] Approve secret mounts and service-file permissions without embedding credentials in images.
* [ ] Test initial connection, pool renewal, certificate trust, and restart without an interactive login.
* [ ] Define local role membership, object ownership, and security-definer search paths.
* [ ] Prove the runtime reader cannot write evidence or inspect unrelated application state.

Pass P4 for staging when export and restore identities, secure storage, and the local role map
are ready. Runtime pool changes and Azure token-renewal tests can remain pending for an isolated
restore, but must be tracked as P7 blockers before starting the application or switching traffic.

## P5 Consistent export and transfer readiness

### Select a consistency strategy

The implemented choice is option 1 for the whole database: stop every writer, then let the
`db-export` job prove the freeze by comparing write counters before, during, and after the dump.

`pg_dump` provides a consistent database snapshot, but a separate count or digest query does not
automatically use that same snapshot. Blob objects are not included in the database transaction.

Choose one approach and record it:

1. Freeze changes to the selected release and its referenced artifacts for the baseline, export,
   and source-manifest capture window. Resume only through the agreed release procedure.
2. Keep writes running, but coordinate an exported PostgreSQL snapshot for the dump and database
   manifests. Keep the exporting transaction open until all consumers have imported it. An
   operator-tested snapshot procedure is required; unrelated before-and-after scans are insufficient.

Either approach needs immutable or version-pinned blob references. Retain the referenced blob
versions under the approved retention policy. Record any mutable paths and how they are stabilized.

### Prepare the transfer

* [ ] Approve the export window, source-load limits, and cancellation criteria.
* [ ] Confirm a source recovery point and ownership of the existing Azure recovery process.
* [ ] Confirm the destination database is disposable, isolated, and not serving users.
* [ ] Test the transfer path with a non-sensitive file and measure throughput.
* [ ] Select encryption in transit and at rest, key custody, retention, and cleanup ownership.
* [ ] Approve an independent recovery destination; Spark 2 alone does not cover loss of the site.
* [ ] Prepare source manifests: projection IDs, payload digests, vector fingerprints, and graph counts.

Use sorted manifests with deterministic encoding and ordering. Payload digests validate projected
evidence, not necessarily every vector or graph object. Capture embedding dimensions and a
separate deterministic vector fingerprint or full vector comparison. Do not put document text
in a manifest unless explicitly required and approved.

Record measured bytes per second and estimate transfer duration from the actual archive size.
Do not promise a migration duration from row count alone.

Pass P5 when consistency, artifact retention, secure transfer, capacity, and recovery preparation
are approved. Sign the staging authorization before executing the following procedure.

## Approved staging export and restore

> [!CAUTION]
> This section writes archive files and creates or changes the isolated DGX database. It is not
> a read-only preflight. Run only after P0-P5 approval. Never point `RESTORE_SERVICE` at Azure or
> an existing production database. Do not add `--clean`, `--create`, or destructive cleanup flags.
> `REPLACE_DATABASE=1` makes `restore.sh` drop the named staging database first; never set it for
> a database you still need.

### Export with the db-export job

The export runs in AKS as the Entra administrator (`id-crv-aks-workload`), which owns the CRV
objects. A native sidecar keeps a current Entra token in the libpq password file. The export
container uses the PostgreSQL 16.15 client tools that match the source. The upload container then
copies the result to the private Blob container `crv-migration` under `<RunId>/<database>/`.
Use a new run ID with second resolution for every job.

Run the modes in order: `inventory` (read-only evidence), `schema` (schema-only dump), then `full`
inside the approved freeze window only.

```powershell
$run = 'f' + (Get-Date -Format 'MMddHHmmss')
./infra/kubernetes/pipeline/render-pilot.ps1 -Job db-export -ExportMode full `
  -ExportDatabase ingestion-catalog -RunId $run -DeadlineSeconds 7200 `
  -OpsClientId REPLACE_OPS_CLIENT_ID -PipelineDigest REPLACE_PIPELINE_DIGEST `
  -ClientDigest sha256:537d0998e20878d435e5f4bd2f672dc5400a2f832ee3b9c0625f71437d2bde43 -Invoke
```

In `full` mode, [export.sh](../infra/kubernetes/pipeline/db-export/export.sh) works in this order:

1. It records the write counters of every table and every sequence position, waits
   `-ExportSettleSeconds` (default 60), records them again, and stops with exit 4 if they differ.
2. It writes the inventory, the manifest with a row digest of every table, and the chunk list.
3. It runs `pg_dump --format=custom --compress=zstd --lock-wait-timeout=10s`, keeping owners and
   grants.
4. It waits again and stops with exit 5 if any counter moved while the export ran.
5. It writes `SHA256SUMS`, verifies it, and marks the directory complete. The upload refuses to
   overwrite an existing blob.

Stop every writer before a full export. Keep the pipeline at zero replicas with KEDA paused. The
web application may keep running: it reads `ingestion-catalog` with read-only transactions.
Discard a bundle whose freeze check failed. A PostgreSQL dump does not contain external blob
data, server configuration, or passwords.

### Transfer and verify

The storage account accepts only private-endpoint traffic, and a governance policy keeps public
network access disabled. dgx-niz01 therefore fetches the bundle through the operator's
point-to-site VPN: an SSH reverse tunnel from the laptop forwards a DGX loopback port to the Blob
private endpoint. TLS runs end to end between the DGX and Azure, so the laptop relays only
encrypted bytes and keeps no copy. First check that the Blob name resolves to the private endpoint
(`10.0.8.12`) and that Global Secure Access does not override the private-link DNS.

Keep the tunnel open in one terminal while the fetch runs:

```powershell
ssh -N -o ExitOnForwardFailure=yes -R 127.0.0.1:18443:10.0.8.12:443 dgx-niz01
```

In a second terminal, pass a short-lived storage token over SSH into a `0600` curl config on the
encrypted volume, run [fetch-bundle.sh](../infra/dgx/staging/fetch-bundle.sh) from the DGX
repository checkout, and delete the config again:

```powershell
$token = az account get-access-token --resource https://storage.azure.com/ --query accessToken -o tsv
"header = `"Authorization: Bearer $token`"", 'header = "x-ms-version: 2023-11-03"',
  'connect-to = "stawmc6icu2kueq.blob.core.windows.net:443:127.0.0.1:18443"' |
  ssh dgx-niz01 "umask 077; tr -d '\r' > /srv/crv-secure/secrets/blob-fetch.curlrc"
ssh dgx-niz01 "cd REPLACE_DGX_REPO_DIR && bash infra/dgx/staging/fetch-bundle.sh $run /srv/crv-secure/secrets/blob-fetch.curlrc 4; rm -f /srv/crv-secure/secrets/blob-fetch.curlrc"
```

The script downloads each large file in 4 parallel ranges through small HTTP range requests
(`CRV_FETCH_CHUNK`, 1 MiB by default), so little data queues inside the VPN. Each range resumes
from the bytes it already holds after a stall, a VPN reconnect, or a rerun. After a host restart,
the last writes of a range can be lost while its length survives, so a range written before the
current boot first drops its last 64 MiB, and a file that is already complete is hashed again
before it is skipped. The script checks every size, runs `sha256sum --check --strict SHA256SUMS`,
and only then moves the bundle from `.incoming-<run>` to `/srv/crv-secure/bundles/<run>`. It
refuses to overwrite an existing bundle.
Rewrite the config with a new token if a transfer outlasts it. If the VPN stops passing traffic,
reconnect it; the fetch continues where it stopped.

Checksums detect corruption, not an untrusted sender. Never copy a bundle that holds row data
anywhere on the DGX outside `/srv/crv-secure`. Delete the Blob copy once the DGX copy is verified
and the retention agreed under P5 ends.

### Restore on the isolated target

Create the encrypted volume once, then open it after every reboot. Both commands need `sudo` and
the operator's passphrase, so run them yourself in an interactive SSH session:

```bash
sudo bash infra/dgx/staging/luks-volume.sh create 100G
sudo bash infra/dgx/staging/luks-volume.sh open
```

Build the image on dgx-niz01 and start the staging server. It listens on the DGX loopback only,
reads its generated superuser password from the volume, and does not restart by itself:

```bash
docker build -t crv-postgres16:age1.6.0-pgvector0.8.2 infra/dgx/postgres16
CRV_PG_IMAGE=crv-postgres16:age1.6.0-pgvector0.8.2 CRV_SECURE=/srv/crv-secure \
  docker compose -f infra/dgx/staging/compose.yaml up -d
```

Then restore the bundle into a new database named after its source and run the P6 checks:

```bash
export CRV_BUNDLE_ROOT=/srv/crv-secure/bundles CRV_RESTORE_LOGS=/srv/crv-secure/restore-logs
bash infra/dgx/staging/restore.sh restore "$CRV_BUNDLE_ROOT/REPLACE_RUN_ID/ingestion-catalog"
CRV_PIPELINE_IMAGE=crv-pipeline:4520b64-arm64 bash infra/dgx/staging/verify-staging.sh
```

[restore.sh](../infra/dgx/staging/restore.sh) stops at the first failure and keeps its evidence
under `CRV_RESTORE_LOGS`, which must point to `/srv/crv-secure/restore-logs` for real data: the
manifests and chunk lists hold row digests. It runs these steps:

1. Verify the checksums and that `pg_dump` 16 made the archive.
2. Reject publications, subscriptions, event triggers, large objects, and any extension outside
   the allow-list. `SKIP_EXTENSIONS` comments out Azure-only extensions; Azure's own privilege
   changes on `pg_catalog` objects are always left out.
3. Run [prepare-target.sql](../infra/sql/migration/prepare-target.sql): NOLOGIN placeholder roles
   without passwords, their memberships except grants of predefined roles, a new database with
   the source encoding, locale, and owner, and the database grants and per-role settings that
   `pg_dump` does not carry. An existing database is refused.
4. Run `pg_restore --exit-on-error --single-transaction --no-security-labels`.
5. Repair the AGE graph IDs, then run `vacuumdb --analyze` after a full restore.
6. Compare the target manifest and chunk list with the source and fail on any difference.

A failed `pg_restore` rolls back its single transaction but leaves the empty database that step 3
created. Fix the cause, then rerun with `REPLACE_DATABASE=1` so the script drops that database
first. Keep the container's `shm_size` above `maintenance_work_mem`: a parallel HNSW build needs
one shared memory segment of that size.
Restored roles cannot log in. Before any application connects, create local logins and grant
them the restored group roles they hold on Azure, such as `tizert_ingest_worker` or
`crv_kg_reader`. Never grant a login membership in the owner role. Keep application services and
replication disabled during the first restore. A single-transaction restore needs WAL and disk
headroom and cannot run in parallel; change that only after a measured rehearsal.

## P6 Restore acceptance checks

### Data and vector integrity

Run the following using a target audit identity. Direct table inspection is not intended for
the execute-only application reader.

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '60s';
SELECT * FROM crv_retrieval.release_status('crv-v1');
SELECT count(*) AS chunks,
       count(embedding_te3) AS te3_vectors,
       count(*) FILTER (WHERE embedding_te3 IS NULL) AS missing_te3,
       count(*) FILTER (
           WHERE embedding_te3 IS NOT NULL AND public.vector_dims(embedding_te3) <> 3072
       ) AS wrong_te3_dimensions
FROM crv_retrieval.chunk
WHERE release_id = 'crv-v1';
SELECT projection_id, payload_sha256
FROM crv_retrieval.chunk
WHERE release_id = 'crv-v1'
ORDER BY projection_id COLLATE "C";
COMMIT;
```

Compare full sorted source and target manifests from the same P5 snapshot. A matching count or
an aggregate hash alone is insufficient to diagnose missing, extra, or mismatched rows. For the
initial TE3 move, `embedding_m3` may be empty; do not fail it unless M3 is explicitly in scope.

| Check | Acceptance |
|-------|------------|
| Release counts | Exact snapshot match; historical baseline is 145,855 chunks |
| Projection IDs and payload digests | Zero missing, extra, or different entries |
| TE3 coverage | One non-null 3,072-dimensional vector for every in-scope chunk |
| Vector fingerprints | Exact deterministic source/target match; diagnose any difference |
| Provenance | Chunk IDs, content hashes, document codes, locators, and original pages preserved |
| Citation artifacts | Representative authenticated Blob reads succeed with expected checksums |
| Graph and catalog | Included relations and AGE graph labels/counts match the source manifest |
| Applied migrations | Ledger and checksums match the shipped revision and source snapshot |

### Indexes, permissions, and graph behavior

```sql
SELECT indexrelid::regclass AS index_name, indisvalid, indisready
FROM pg_index
WHERE indrelid = 'crv_retrieval.chunk'::regclass
ORDER BY indexrelid::regclass::text;
SELECT has_schema_privilege('crv_kg_reader', 'crv_retrieval', 'USAGE') AS schema_usage,
       has_function_privilege(
           'crv_kg_reader', 'crv_retrieval.release_status(text)', 'EXECUTE'
       ) AS status_execute,
       has_table_privilege('crv_kg_reader', 'crv_retrieval.chunk', 'SELECT') AS direct_select,
       has_table_privilege('crv_kg_reader', 'crv_retrieval.chunk', 'INSERT') AS direct_insert;
```

Expect valid, ready indexes. For the retrieval reader, expect schema usage and approved function
execution, with direct table SELECT and INSERT denied. Also test UPDATE, DELETE, unrelated
schemas, and PUBLIC grants. Test with the actual application login, not only the group role.

* [ ] Use query plans to prove HNSW for unscoped vector search and the appropriate scoped index path.
* [ ] Compare ANN and exact top-50 results at the same parameters; require recall at least 0.95.
* [ ] Include representative golden queries, not only the earlier 10 stored-vector samples.
* [ ] Test identifier lookup, French accents, English stemming, empty queries, and hash scoping.
* [ ] Test table neighbors and their original page and source attribution.
* [ ] Run an existing application graph query with the final AGE search path and read identity.
* [ ] Confirm unauthorized writes fail without changing data.

Pass P6 when all included objects, vectors, access boundaries, and source references are verified.

## P7 Application and retrieval readiness

### Runtime portability

* [ ] Pin native ARM64 backend, frontend, database, and optional GPU-serving images by digest.
* [ ] Validate native Python dependencies, including PostgreSQL drivers, in the actual image.
* [ ] Supply and test local database authentication for every connection that moves to DGX.
* [ ] Inventory any state or graph connection intentionally left in Azure and test its private path.
* [ ] Verify non-AKS Azure credential acquisition, token refresh, and certificate rotation behavior.
* [ ] Configure the release, pool size, timeouts, TLS CA, embedding deployment, and backend selection.
* [ ] Use the same TE3 embedding model and 3,072 dimensions for initial query embeddings.
* [ ] Confirm citations and source-document links work from the intended client network.
* [ ] Ensure no ingestion worker or scheduler starts against the restored catalog unintentionally.
* [ ] Prove the fallback configuration can still select Azure AI Search without rebuilding images.

Review the current
[runtime settings](../src/backend/crv_agent/runtime/settings.py)
and factory at the exact revision used for the rehearsal. As of 2026-10-28 the backend supports
password-file PostgreSQL logins with an explicit port and TLS mode, separate memory connection
settings, local OpenAI-compatible model, embedding and reranker endpoints, M3 search, local source
PDFs, and no Azure credential when nothing needs one. These paths have unit and integration tests
but have not run on the DGX. The NVIDIA reranker option does not exist.

### Keep model changes separate

The first restore uses the existing TE3 vectors. Adding BGE-M3 changes the embedding space;
switching the query encoder alone makes the stored vectors incompatible. Populate the separate
M3 column, build its own index, and evaluate it as a later change.

If the NVIDIA reranker is selected, verify the exact model and serving runtime on GB10. Hosted
API results do not demonstrate ARM64 container availability, model-license approval, local
latency, or supported rerank request shape. Compare the same input passages and document order.
See [serving optimization guidance](./DGX_SPARK_SERVING_OPTIMIZATION.md) for memory and runtime
considerations. GPU readiness is not required for a TE3 database-only staging restore.

### Evaluation gates

Run the source and DGX configuration in the same comparison window with pinned questions,
release, prompts, model deployments, and reranker settings. Preserve raw results and judge
errors. Do not substitute offline anchor recall for a missing judge-based gate.

| Metric | Required result before production adoption |
|--------|--------------------------------------------|
| A1 judge@10 | No more than 0.03 below the matched Azure baseline |
| CEIL anchor@50 | No more than 0.02 below the matched Azure baseline |
| Citation contract | Unchanged from Azure, including source IDs, locators, and page attribution |
| ANN versus exact recall@50 | At least 0.95 on the approved query set |
| End-to-end answers | Completed dev evaluation with no unapproved correctness or citation regression |
| Latency and concurrency | Meet an application-owner-approved p50/p95 and sustained-load budget |

Agree the latency budget before testing; there is no production DGX SLO established by this
runbook. Report embedding, database, reranker, generation, and network time separately. Measure
cold and warm queries, unseen questions, expected concurrency, memory, disk, and GPU contention.

The existing
[pipeline renderer](../infra/kubernetes/pipeline/render-pilot.ps1)
supports read-only `retrieval-verify` checks: `preflight`, `parity`, `recall`, and `explain`.
`-RetrievalQuestions` also generates query embeddings and can incur charges. On the DGX, run the
same checks from the ARM64 pipeline image with `TIZ_DB__DSN` and a local login, as
[synthetic-rehearsal.sh](../infra/dgx/staging/synthetic-rehearsal.sh) does.

Pass P7 only when the final application image, authentication path, selected reranker, quality
gates, and measured load budget pass together. Pending evaluation jobs are not a PASS.

## P8 Backup and operations readiness

The original topology puts the primary database and application on Spark 1, with a standby,
backups, and evaluation on Spark 2. Confirm that allocation against measured CPU/GPU memory
contention rather than treating it as mandatory placement.

* [ ] Define and approve the recovery point objective and recovery time objective.
* [ ] Configure encrypted backups with retention, ownership, failure alerts, and restricted access.
* [ ] Restore a backup into a second disposable database and verify P6 checks there.
* [ ] Keep an independent recovery copy outside the two-machine failure domain.
* [ ] If using a standby, verify streaming state, lag, WAL retention, and read-only enforcement.
* [ ] Test promotion, fencing of the old primary, client rerouting, and recovery from split-brain risk.
* [ ] Confirm Compose or systemd startup order, readiness checks, restart policy, and persistent volumes.
* [ ] Restart containers and reboot a host in staging; confirm data and service recovery.
* [ ] Alert on disk space, WAL growth, database availability, backup age, replication lag, and certificate expiry.
* [ ] Monitor query p95, timeouts, connection saturation, token failures, memory pressure, and GPU failures.
* [ ] Define support ownership, log access, on-call escalation, and incident procedures.

A standby is not a backup: it also replicates accidental changes. Two hosts at the same site
do not provide site-level disaster recovery. A backup job that has never been restored does not
satisfy P8. Do not claim automatic failover unless it is implemented and tested.

Pass P8 when recovery is demonstrated within the approved objectives and operators can detect
and handle the relevant failures.

## P9 Release synchronization, cutover, and rollback

### Parallel validation and source of truth

Keep Azure ingestion authoritative during the initial move. The existing PostgreSQL retrieval
copy is a release backfill, not proof of ongoing dual writes or an automatic DGX synchronization
process. Define how a new release, catalog changes, and referenced blobs reach DGX.

* [ ] Record the release advertised by each backend and detect any release mismatch.
* [ ] Define ownership and verification for each incremental copy or full release refresh.
* [ ] Test release activation as an explicit operation, not a partially updated live table.
* [ ] Keep prior release data and configuration long enough for the agreed rollback window.
* [ ] Run approved parallel comparisons without routing uncontrolled production data externally.
* [ ] Complete the final source-versus-DGX comparison after the last synchronized release.

### Cutover approval

* [ ] Application owner accepts final quality, citation, and latency reports.
* [ ] Database owner accepts parity, security, backups, and synchronization evidence.
* [ ] Security owner accepts data location, identities, network exposure, and model usage.
* [ ] Operations owner accepts monitoring, runbooks, recovery objectives, and support coverage.
* [ ] Change owner approves a window, traffic scope, rollback triggers, and observer.
* [ ] Test the exact routing or configuration change and reversal in staging.

### Rollback contract

Keep Azure AI Search, its permissions, and the previous backend configuration intact. Agree
numeric triggers for errors and latency, plus immediate triggers for incorrect release routing,
data-integrity failures, or broken citations. Name the person authorized to revert.

The first rollback should restore the prior application routing or backend selection, not delete
DGX data. Confirm Azure routing and user-visible answers after reversal, then preserve DGX
logs and artifacts for diagnosis.

If users or workers write state to DGX after cutover, reverting retrieval does not automatically
move that state back to Azure. Define reconciliation before admitting writes. Retiring Azure
Search, removing source data, and decommissioning identities require separate approval after
the rollback window closes.

Pass P9 when release freshness, the production change, state handling, and rollback have all
been demonstrated and signed off.

## Stop conditions

Stop the affected operation and preserve evidence when any of these occurs:

* The source and target endpoints or release identity cannot be distinguished reliably.
* A dump, restore, checksum, manifest comparison, or migration-ledger check fails.
* An export requires unapproved public access, disabled TLS validation, or elevated source privileges.
* Native ARM64 support or a required PostgreSQL extension is missing.
* Source resource use exceeds the approved operational threshold.
* Target disk or memory headroom falls below the approved floor.
* Roles, PUBLIC grants, or security-definer ownership expose more data than approved.
* Referenced blobs are unavailable, mutable without retention, or outside the approved data scope.
* The application cannot authenticate noninteractively after a restart or credential renewal.
* Quality gates are incomplete, a required evaluation has errors, or rollback is unavailable.

An application or quality blocker need not cancel an already approved isolated restore rehearsal.
It does block application activation or production cutover. Reopen only the relevant gate after
the defect is resolved; never relabel an unresolved failure as a warning to continue.

## Sign-off record

Complete one record per export and restore attempt. Store sensitive evidence in the approved
restricted location, not in this repository.

| Field | Value |
|-------|-------|
| Run ID and UTC date | TBD |
| Migration lead and approvers | TBD |
| Source server, database, release, snapshot method | TBD |
| Source Git revision and applied migration ledger | TBD |
| Primary and secondary DGX hostnames | TBD |
| PostgreSQL, extension, image, driver, and runtime versions | TBD |
| Approved data scope and Blob retention policy | TBD |
| Capacity, source-load limits, and transfer measurements | TBD |
| Archive size, checksum, and evidence location | TBD |
| Target database identity and role-map revision | TBD |
| P0-P5 decision and approval timestamp | NOT CHECKED |
| Restore, parity, index, graph, and permission results | NOT CHECKED |
| Backend and reranker image digests | TBD |
| Quality reports and latency budget | TBD |
| Backup restore and failover evidence | TBD |
| Release synchronization and rollback evidence | TBD |
| P6-P9 decision and approval timestamp | NOT CHECKED |
| Exceptions, owner, expiry, and residual risk | None approved |

## References

* [CRV retrieval migration plan](./PGVECTOR_RETRIEVAL_TEST_PLAN.md)
* [DGX model-serving guidance](./DGX_SPARK_SERVING_OPTIMIZATION.md)
* [Edge placement](./DGX_EDGE_PLACEMENT.md), [edge runbook](./DGX_EDGE_RUNBOOK.md),
  [edge validation kit](./DGX_EDGE_VALIDATION.md) and [edge identity](./DGX_EDGE_IDENTITY.md)
* [Retrieval schema](../src/ingestion/ingest_pipeline/sql/0022_crv_retrieval.sql)
* [Retrieval query-plan migration](../src/ingestion/ingest_pipeline/sql/0023_crv_retrieval_plans.sql)
* [BM25 migration](../src/ingestion/ingest_pipeline/sql/0025_crv_retrieval_bm25.sql)
* [BM25 shortlist migration](../src/ingestion/ingest_pipeline/sql/0026_crv_retrieval_bm25_shortlist.sql)
* [Keyword-term migration](../src/ingestion/ingest_pipeline/sql/0027_crv_retrieval_keyword_terms.sql)
* [PostgreSQL logical backup](https://www.postgresql.org/docs/16/app-pgdump.html)
* [PostgreSQL logical restore](https://www.postgresql.org/docs/16/app-pgrestore.html)
* [PostgreSQL connection service files](https://www.postgresql.org/docs/16/libpq-pgservice.html)
* [Azure PostgreSQL dump and restore](https://learn.microsoft.com/azure/postgresql/migrate/how-to-migrate-using-dump-and-restore)
* [Azure PostgreSQL pg_dump and pg_restore best practices](https://learn.microsoft.com/azure/postgresql/flexible-server/how-to-pgdump-restore)
* [Apache AGE graph IDs after pg_restore](https://github.com/apache/age/issues/2503)
* [DGX Spark hardware specifications](https://docs.nvidia.com/dgx/dgx-spark/hardware.html)
