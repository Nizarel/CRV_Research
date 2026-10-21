# CRV_Research

Evidence-first ingestion of the CRV research corpus in `Data\DATAROOM CRV`.
The durable pipeline is in [src/ingestion](src/ingestion): PostgreSQL owns jobs,
leases, retries, extraction barriers and the transactional outbox; Service Bus
wakes workers. Source bytes and normalized evidence are content-addressed.
Search uploads are verified before a projection is marked complete.

## CRV adaptations

- **Offline intake:** Windows extended-length paths, content sniffing rather than
  extension-only routing, explicit per-input outcomes, retained originals,
  bounded ZIP expansion and isolated legacy Word/PowerPoint conversion.
- **Scientific PDFs:** layout/OCR baseline for nonblank pages, original-page
  citations, and optional selective native-text routing. Images, vector-rich
  pages and detected multiple columns keep the layout route.
- **Large sources:** 1 GiB source ceiling with streaming downloads; 40 MiB default
  provider submissions. Oversized PDFs are split using the actual serialized
  subset sizes, not a page-size estimate. An oversized single page is recorded
  as unsupported, never submitted. Partial evidence is not projected by default.
- **Tables:** preserved cells, captions, headers and footnotes. The reviewed flora
  workbook uses independent site columns, not cross-site row relationships.
- **Research metadata:** collections, sites, document types, report codes and
  original-file lineage replace equipment/zone metadata in the CRV Search index.
  Filing context is marked `metadata_basis=path`. Report codes take precedence in
  this order. A code printed in the running page header on most pages is marked
  `document_code_basis=printed_header`, and every chunk of the document inherits it.
  A code found in the chunk's own text is marked `printed`, and one from the path is
  marked `path`. The OCR misreading of `CRAR/` as `CRIAR/` is repaired.
- **Running headers and footers:** lines repeated on at least 30% of a document's
  pages (minimum 3) are dropped from chunk text and section paths, even when DI
  labels them as paragraphs or headings. The document title is kept.
- **Optional observations:** numeric table-cell observations retain raw values,
  qualifiers, explicit units, headers, footnotes and locators. They are
  `unreviewed`, not certified measurements or inferred chemistry.

The local corpus inspection found 1,967 files, about 24 GiB, predominantly PDFs
with 47 legacy DOC/PPT files and one ZIP. Some names disagree with the bytes;
305 paths were missed by ordinary Windows `is_file()` checks. Historical scanned
reports, two-column papers, experimental tables and commercial LIMS documents
require different handling. These are inventory/sample findings, not OCR quality
certification for the full corpus.

## Local setup

### Direct Nextcloud raw backup

[The AKS transfer job](infra/kubernetes/corpus-transfer/job.yaml) copies only
`DATAROOM CRV` to `stawmc6icu2kueq/crv-raw/DATAROOM CRV`, independently of the
ingestion pipeline. Its [runner](infra/kubernetes/corpus-transfer/transfer.py)
uses Nextcloud browser approval: log in only on the Managem website using the
`AUTHORIZE_IN_BROWSER` link in the pod log. No credentials belong in manifests,
chat, or local files. The generated app password remains in pod memory and is
revoked on normal completion or failure. If the pod is killed or its node is lost,
revoke **CRV-Corpus-Transfer** manually in Nextcloud's security settings.

Prerequisites are the private `crv-raw` container, the dedicated
`id-crv-corpus-transfer` identity with Blob Data Contributor scoped to that
container, and its federation to `system:serviceaccount:crv-transfer:corpus-transfer`.
Both pinned runtime images must exist in the private ACR. The server currently
omits a required GlobalSign intermediate: populate ConfigMap `corpus-transfer-ca`
with `nextcloud-ca.pem` containing the CA chain exported from a successfully
validated TLS connection. The job adds it to the system CA bundle; it never
disables certificate verification. Render `${TRANSFER_JOB}`
to a unique job name, apply the namespace/service account, create the
`corpus-transfer` ConfigMap from the runner, then apply the rendered job.
Use the `aks-crv-southcentralus` cluster explicitly, not a stale kubeconfig context.

Files stream directly between services. Copying never deletes blobs and refuses
to overwrite different existing files. A new job resumes by skipping matching
files, but requires browser approval again. Conflicting files require explicit
operator review; they are not silently replaced. Verification rereads all source
and destination bytes (`rclone check --download --one-way`) and checks the source
path/size/modification-time inventory has not changed. It does not reject extra
destination files or provide a transactional Nextcloud snapshot. This second
pass approximately doubles source traffic. A verified inventory is retained at
`crv-raw/_transfer-reports/<job-name>.json`. No OCR or indexing is triggered.

Python 3.11 or 3.12 is supported. From the repository root:

```powershell
Set-Location .\src\ingestion
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[azure,parsers,tokenizer,dev]"
.\.venv\Scripts\python.exe -m ingest_pipeline.cli --help
```

`crv-pipeline` and the compatibility name `tizagent-pipeline` are installed entry
points. The Python package is `ingest_pipeline` (distribution: `ingest-pipeline`).
The `TIZ_*` environment variables and compatibility CLI name are retained
for existing operational integrations. Optional `tools`
dependencies are only needed for the standalone discovery utility.

## 1. Prepare locally, without Azure calls

Use a short, dedicated output path **outside** the corpus. Allow at least 64 GiB
of free storage for staging and conversion overhead; actual usage depends on
duplicate bytes and converted files.

```powershell
.\.venv\Scripts\python.exe -m ingest_pipeline.cli prepare-corpus `
  --root "C:\Github\CRV_Research\Data\DATAROOM CRV" `
  --output "C:\CRV-intake" --max-total-gib 64
```

This command does not configure Azure telemetry or start cloud clients. It writes:

- `objects\<sha256>`: verified immutable originals and derivatives.
- `manifest.json`: original relative paths, archive members/ancestor hashes,
  detected formats, content identities, conversion image and outcomes.

Outcomes are `ready`, `converted`, `expanded`, `excluded`, or `failed`. Blank
placeholders and unsupported formats are explicitly excluded. A failed input
produces an error log and exit code 2; publication refuses failed or incomplete
manifests. **Review exclusions too:** zero failures is not proof that every file
is scientifically usable. Directory enumeration errors and links are not
silently ignored.

ZIP members are read as streams into content-addressed objects, never extracted
to member-supplied filesystem paths. Defaults bound expansion to 1 GiB per
archive, 20,000 members, depth 2 and a 200:1 expansion ratio. Nested members also
consume the global intake budget. Encrypted, unsafe or corrupt inputs fail
explicitly. Re-running preparation verifies existing objects and reuses cached
conversions for identical input bytes and image identity.

### Legacy Office conversion

Legacy `.doc`/`.ppt` inputs fail explicitly unless `--office-image` is supplied.
When a converter is supplied, `.docx`/`.pptx` files are also rendered to PDF.
This makes scanned pages pasted into Word OCR-able and gives page citations.
Without a converter they are still analyzed natively.
Install/start Docker with Linux containers, then build the converter locally:

```powershell
docker build -t crv-office:local .\office-converter
$image = docker image inspect crv-office:local --format '{{.Id}}'
.\.venv\Scripts\python.exe -m ingest_pipeline.cli prepare-corpus `
  --root "C:\Github\CRV_Research\Data\DATAROOM CRV" `
  --output "C:\CRV-intake" --office-image $image
```

The image must already exist locally, pinned by `sha256:<image-id>` or
`repository@sha256:<digest>`. Runtime pulls and network access are disabled;
the root filesystem is read-only, capabilities are dropped, and time, memory,
process and output limits apply. LibreOffice macros are disabled. Image building
requires access to its base image/package repositories; preparation itself does
not. Conversion preserves the original bytes. Converted page references are
**derived-PDF pages**, not native Word or PowerPoint pagination.

Actual LibreOffice rendering still requires a representative visual pilot.
Isolation and byte-signature checks do not prove conversion fidelity.

## 2. Select a policy and review the manifest

[crv-policies.json](infra/content-understanding/crv-policies.json) provides:

| Policy | Intended use |
|---|---|
| `crv-layout-v1` | Conservative pilot baseline: layout/OCR on nonblank PDF pages |
| `crv-selective-v1` | Native text for clean prose; layout for detected structural risks |
| `crv-cu-layout-v1` | Comparison arm only: CU `prebuilt-layout` extraction, no generative features |

Only `crv-cu-layout-v1` routes to Content Understanding. It uses CU's prebuilt
layout analyzer, which calls no completion model. Extraction-only analyzers
therefore run under a token budget of zero, and only the page budget limits them.
Analyzers with completion models still reserve their estimated tokens.
The reviewed CU registry is [crv-cu-registry.json](infra/content-understanding/crv-cu-registry.json).
The default configuration uses the layout baseline.

The reviewed `Flore Managem.xlsx` archive member has SHA-256
`84e42620e08e0bde04a1b34326abc115fc9a74b666c4a238c4a77ac8aaf96e97`.
Both policies recognize that exact content hash, use row 2 as the final header
row and keep columns B/C/D separate. Changed workbook bytes require a fresh
review and an updated allowlist; other workbooks retain ordinary table behavior.

Scientific observation artifacts are opt-in:
`TIZ_CHUNKING__SCIENTIFIC_OBSERVATIONS=true`. They are JSONL artifacts linked to
the chunk-stage outcome, not a new result database or Search measurement index.
Ambiguous grouped/decimal values such as `1,234` and `1.234` are omitted from
numeric observations; the source table remains intact. Units are not inferred
from headers. No chemical meaning or cross-row relationship is invented.

## 3. Configure infrastructure before publication

Existing Azure/PostgreSQL resources and permissions are prerequisites. Configuration
is defined in [config.py](src/ingestion/ingest_pipeline/config.py).
Typical settings include:

```powershell
$env:TIZ_DB__HOST = "<postgres-host>"
$env:TIZ_DB__USER = "<database-principal>"
$env:TIZ_BLOB__ACCOUNT_URL = "https://<account>.blob.core.windows.net"
$env:TIZ_BLOB__SOURCE_CONTAINER = "<dedicated-crv-source-container>"
$env:TIZ_BUS__NAMESPACE = "<namespace>.servicebus.windows.net"
$env:TIZ_DI__ENDPOINT = "https://<document-intelligence-endpoint>"
$env:TIZ_EMBEDDINGS__ENDPOINT = "https://<openai-endpoint>"
$env:TIZ_EMBEDDINGS__DEPLOYMENT = "<text-embedding-3-large-deployment>"
$env:TIZ_SEARCH__ENDPOINT = "https://<search>.search.windows.net"
$env:TIZ_SEARCH__INDEX_NAME = "crv-evidence-v1"
$env:TIZ_SEARCH__CORPUS = "crv"
$env:TIZ_POLICIES_PATH = "C:\Github\CRV_Research\infra\content-understanding\crv-policies.json"
$env:TIZ_ROUTING_POLICY = "crv-layout-v1"
```

Use managed/workload identity or `DefaultAzureCredential`; do not commit secrets.
Apply migrations **before** running the new workers:

```powershell
.\.venv\Scripts\python.exe -m ingest_pipeline.cli migrate --dry-run
.\.venv\Scripts\python.exe -m ingest_pipeline.cli migrate
```

Migration `0014_ingest_source_provenance.sql` adds nullable JSON lineage to source
snapshots. Existing snapshots remain valid. Handler/policy/chunker identities
version changed outputs; do not mix old and new worker images in one rollout.

Create a **separate** index using
[crv-evidence-index.json](infra/search/crv-evidence-index.json), replacing
`${EMBEDDING_RESOURCE_URI}` and `${QUERY_EMBEDDING_DEPLOYMENT}` with your approved
query-embedding configuration. Vectors must have 3,072 dimensions.
Do not apply CRV rows to the old maintenance index.

[crv-knowledge-source.json](infra/search/crv-knowledge-source.json) references the
same index and `crv-evidence-semantic` configuration. Replace `${RELEASE_ID}` with
the chosen policy's release. It deliberately includes `and false`: **serving is
disabled until caller-aware authorization is implemented and tested**. Empty
ACL fields are not automatic Azure Search enforcement. Collection/site filters
are retrieval constraints, not authorization. Identical content can retain
multiple placements, so establish access boundaries before exposing citations.

The Kubernetes renderer defaults to the CRV index and layout policy. It supports
`-RenderPath` for local rendering without cluster access. The IO/CPU worker
concurrency is one; scratch and memory must still be measured during the pilot,
because decompressed parser memory is not bounded by source file size.
Provider budgets in the manifest default to zero: explicitly authorize a small
DI page allowance before processing. Embedding usage also incurs costs.

## 4. Publish only after approval

This command uploads originals, manifests and logical source blobs to Azure,
then enqueues source snapshots:

```powershell
.\.venv\Scripts\python.exe -m ingest_pipeline.cli ingest-corpus `
  --manifest "C:\CRV-intake\manifest.json" --prefix crv
```

Use a dedicated source container/prefix. Publication validates every staged
object before uploading. Source names are stable hashes of logical source paths;
content updates use conditional ETags. Re-running is idempotent and recovers
partially completed publication/enqueue operations. It does not delete sources
removed from a later manifest.

**Do not enqueue the entire source container:** immutable original objects and
manifests live there too. `ingest-corpus` enqueues only its logical source blobs.
The coordinator and workers run through the existing CLI or Kubernetes manifests.
Use `status`, job inspection and replay to investigate failed or review-required
units rather than assuming enqueue success means ingestion completion.

## Pilot runbook (AKS, private network)

All CRV data services and the AKS API server are private. Pilot work therefore
runs as Kubernetes jobs, applied with `az aks command invoke`. Each job is rendered
by [render-pilot.ps1](infra/kubernetes/pipeline/render-pilot.ps1) from
[crv-pilot-jobs.yaml](infra/kubernetes/pipeline/crv-pilot-jobs.yaml), uses images
pinned by digest, and fails if any placeholder remains.

| Identity | Access |
|---|---|
| `id-crv-pipeline-ops` | Read `crv-raw`; write `crv-sources`; Search service contributor; PostgreSQL migration rights granted by an Entra administrator |
| `id-crv-pipeline-coordinator` | Send to the `ingest-*` queues |
| `id-crv-pipeline-worker` | Receive from queues; read `crv-sources`; write artifacts; call DI, CU and embeddings; write Search |
| Office converter / intake pods | No identity, no service-account token, no network (deny-all `NetworkPolicy`) |

Deploy identities, queues and containers with
[ingestion-pipeline.bicep](infra/ingestion-pipeline.bicep) and
[its CRV parameters](infra/ingestion-pipeline.crv.parameters.json).

1. **Select** a sample locally. No Azure calls are made.
   ```powershell
   python -m ingest_pipeline.cli select-pilot --root "...\DATAROOM CRV" --pages 1000 `
     --policies ..\..\infra\content-understanding\crv-policies.json --list pilot.txt --report pilot.json
   ```
   The sample is deterministic for a seed. It includes every required stratum:
   scanned reports, two-column papers, legacy Office, mislabeled files, LIMS
   commercial documents, procedures/templates and spreadsheets. Predicted DI
   pages use the production routing policy. DOC/PPT pages are listed as
   unknown until conversion.
2. **Stage** (`-Job stage`): copy listed paths from `crv-raw` to the work disk,
   verifying size and Content-MD5. `archive.zip!/member` entries copy a single
   member, and archive lineage is then not recorded.
3. **Convert** (`-Job convert`): collect DOC/PPT/DOCX/PPTX files as `<sha256>.<ext>`,
   including top-level ZIP members, and convert them in an isolated pod. Every
   outcome is written to `results.json`.
4. **Intake** (`-Job intake`): `prepare-corpus --office-converted` reads the
   converted PDFs, records the converter digest, and turns missing conversions
   into explicit failures. Review the manifest before publishing.
5. **Migrate** and **index** (`-Job migrate`, `-Job index`): these are dry runs
   by default. Use `-MigrateApply` or `-IndexApply` only after checking the output.
6. **Publish** (`-Job publish`): upload sources under `crv-pilot` and enqueue
   snapshots. Then render `crv-pipeline.yaml` with `-DiPageBudget 1000`.
7. **Compare arms**: `enqueue-arm --policy crv-cu-layout-v1` with
   `-CuPageBudget 1000 -CuTokenBudget 0`, then:
   ```powershell
   python -m ingest_pipeline.cli compare-arms --prefix crv-pilot/ --report compare.json
   ```
   The report shows per-arm page coverage, table and cell counts, failed or
   unsupported units, billed pages and estimated cost. It also lists pages where
   the arms diverge, plus a seeded set of 30 pages for manual review. Coverage is
   only a proxy; the manual review decides.

### Full corpus run

The same jobs run the full corpus. A few settings differ:

- `-Job stage -AllBlobs` stages every blob under `DATAROOM CRV` instead of a list.
- `-WorkGib 128` sizes the work disk, and `-DeadlineSeconds 21600` allows up to
  6 hours per job.
- `-SourcePrefix crv` sets the source prefix.
- The pipeline is rendered with `-RoutingPolicy crv-full-v1 -SearchReleaseId crv-v1`.
  The full run therefore projects into its own Search release, separate from the
  pilot releases.

`crv-full-v1` uses the same Document Intelligence layout settings as the pilot's
`crv-layout-v1`, which it supersedes.

For bulk runs, render with `-IoConcurrency 3 -IoMaxReplicas 12` and set
PostgreSQL `max_connections` to at least 150. The I/O pool handles DI submission,
embedding and Search verification. That work waits on remote services rather
than using CPU, so higher parallelism cleared the projection backlog about six
times faster. Expect HTTP 429 throttling from embeddings at 200K TPM; 600K TPM
was used during the run and set back to 200K afterwards.

Results of the 2026-10-21 run:
- **Inputs:** 1,967 blobs, giving 1,929 distinct contents, and 46,584 DI pages,
  exactly as predicted.
- **DI spend:** $465.84. This subscription paid $260.00 and `manadry` paid $205.84.
- **Output:** 145,855 rows in Search release `crv-v1`; 96K of them carry a
  `printed_header` document code.

The subscription has an external automatic shutdown, around 01:00 local time,
that stops both AKS and PostgreSQL. After restarting both, the ledger resumes
without redoing billed pages.

### Additional Document Intelligence account (full run)

DI spend fills this subscription's `budget_usd` first. After that, it continues
with the external accounts in `billing.json`, in order. An external account with its own
`di_max_inflight` also runs in parallel with this subscription. The vault denies public
data-plane access, so store the key with
[set-account-key.ps1](infra/kubernetes/pipeline/set-account-key.ps1). It prompts without
echoing and writes through ARM. Never put the key in files or chat. Add the
secret name to the Bicep parameter `accountKeySecretNames` so the worker identity
can read only that secret. The CRV setup is in
[crv-billing.json](infra/kubernetes/pipeline/crv-billing.json): $260 on this
subscription and $260 on `manadry`, with 8 in-flight requests each. It has this shape:

```json
{
  "budget_usd": 0,
  "di_page_usd": 0.01, "cu_page_estimate_usd": 0.005,
  "cu_meter_usd": { "...": 0 },
  "accounts": [
    { "name": "di-full", "budget_usd": 600,
      "di_endpoint": "https://<resource>.cognitiveservices.azure.com/",
      "key_file": "/var/run/secrets/crv/accounts/di-full", "di_max_inflight": 10 }
  ]
}
```

Render with `-BillingPath billing.json -AccountKeySecrets @{ 'di-full' = '<secret-name>' }
-KeyVaultName kv-awmc6icu2kueq -TenantId <tenant>`. The key is sent only to that
account's endpoint. The endpoint must be reachable from AKS egress.

## Knowledge graph (KG-1, deterministic tier)

The graph is built from what ingestion recorded, with no model calls:
- file placements and lineage (`ingest.source_snapshot`);
- file names: genre, `MM-YYYY` dates, revisions, chapters and annexes;
- report codes in page headers, on the cover and in file names;
- CRV cover fields (Emetteur, Projet, Titre, Objet, Domaine, Attribué à) read from page-1/2 DI evidence.

Every vertex and edge carries evidence and the scope keys of that evidence. Sites and organisations come from a **provisional gazetteer**, [crv-gazetteer.json](src/ingestion/ingest_pipeline/data/crv-gazetteer.json). Its review sheet is [Docs/crv-gazetteer-review.csv](Docs/crv-gazetteer-review.csv). Edges derived from it stay `candidate` until experts approve them. The design is in [KNOWLEDGE_GRAPH_DESIGN.md](Docs/KNOWLEDGE_GRAPH_DESIGN.md) §9.1.

1. **Enable Apache AGE once.**
   - Add `age` to the server's `azure.extensions` and `shared_preload_libraries` parameters. These are in the [Bicep](infra/core/database/postgresql-flexible-server.bicep), and changing them restarts the server.
   - Run `-Job dbsetup -DbApply`. It applies migrations 0015–0017 and grants the ops identity `crv_kg_writer` and `crv_kg_release`.
2. **Build.** Run `-Job kg-build -KgDryRun` first and review the coverage report in the job log. Then run `-Job kg-build` to record the run. Re-running on unchanged inputs records nothing. A changed build supersedes facts it no longer reports.
3. **Release.** `-Job kg-release` freezes the projection, loads AGE graph `crv_research_<hash>`, activates it for `-KgEnvironment` (default `dev`) and prints a `kg-check` smoke test.
4. **Agent access.** Use `kg-grant-reader --login <identity> --object-id <oid>`. The login gets EXECUTE only on the release-pinned, scope-filtered functions:
   - `crv_kg.active_release`, `resolve`, `entity`, `neighbors`;
   - `find_documents` and `document_lineage`.

## Golden-set retrieval evaluation

[golden/crv_golden.jsonl](golden/crv_golden.jsonl) holds the 76 golden questions with categories, atomic key facts and deterministic anchors. Regenerate it with `python -m ingest_pipeline.tools.golden convert --xlsx Docs/QA_REMINEX.xlsx --out golden/crv_golden.jsonl`.

`-Job golden-eval` runs as the worker identity. That identity needs `db-bootstrap --kg-reader`, which the `dbsetup` job passes. The job queries release `-EvalRelease` (default `crv-v1`) in four modes: `bm25`, `hybrid`, `semantic`, and `kg` (steered by `crv_kg.find_documents`).

It scores two things:
- anchor recall at 10/20/50;
- LLM-judged key-fact recall at 10, using `-JudgeDeployment` (default `gpt-5.4-mini`; raise its TPM for the run).

It prints a table and the full report between `GOLDEN REPORT` markers. The baseline is in [Docs/evaluation](Docs/evaluation/golden-retrieval-baseline-2026-10-21.md).

## Research backend (development/evaluation)

[src/backend](src/backend) is a separate FastAPI application using LangChain 1.4.5
and LangGraph 1.2.15. LangChain 1.4.5 was selected because the configured package
feed did not yet provide 1.4.6. It reads the existing evidence index and KG-1;
no corpus reindex, external connector, frontend or new model deployment is required.

The package separates domain contracts, ports, research services, graph nodes,
read-only tools, infrastructure adapters, API and runtime composition. Research
workers have bounded tool/model calls. Scientific claims retain original source
quotations and locators; derived values use deterministic decimal calculations.
Candidate aliases, conflicts and incomplete coverage remain explicit.

Retrieval uses semantic hybrid candidates at depth 50, with soft rank penalties
matching the [retrieval baseline](Docs/evaluation/golden-retrieval-baseline-2026-10-21.md):
10 positions for filename-inferred periodic summaries and 20 for a document's
third/later chunk. These are demotions, not exclusions or hard chunk caps.
Each facet worker receives a deterministic evidence pack: the first 16 candidates
(exact report-code matches promoted, otherwise the focused order) as 1,200-character
query-focused excerpts, plus previews of the other 34. `read_evidence` reads up to
three batches of four candidates (8,000 characters each). Evidence records the exact
character spans shown to the model; claims must quote those spans, and the verifier
receives only them. Citations keep the complete chunk. Truncation and top-k coverage
remain explicit. Summary roles are filename inferences, not validated KG facts.

Pre-call token reservations estimate UTF-8 bytes ÷ 3. Provider-reported usage
enforces the hard `CRV_TURN_TOKENS` limit, now 300,000 by default. A local
50-candidate payload measured about 12k estimated tokens per search and 5k per
four-chunk read, roughly 20–70k per facet. Facets beyond the budget report gaps.
Cost, latency and recall effects need live measurement.

KG boosts use Site, ReportCode and **approved** Organisation entities, never
Projects, while preserving an unrestricted semantic Search leg. Catalogue
navigation remains broad. KG `content_hash` is passed unchanged to Search,
including converted PDF hashes; original hashes in `converted_from` are lineage,
not replacement search keys. The measured focused-semantic gain is modest and
does not establish a quality improvement for this agent without live evaluation.

**Evidence ACL implementation is intentionally deferred.** This is not a
shared/production serving release. No public ingress is provided and the
knowledge source remains deny-all. Entra authentication protects conversation
ownership, not document permissions. The optional local identity is allowed by
the CLI only on loopback; never expose a locally configured app via another server.

```powershell
python -m pip install -e ".\src\backend[dev]"
python -m crv_agent --help
```

Configure the variables described in [the environment example](src/backend/.env.example);
it is a reference, not an automatically loaded secrets file. Supply actual CRV
deployment names for planner, worker, answer and verifier roles (they may share a
deployment). Windows native dependencies may require an x64 Python interpreter.
The CLI selects a psycopg-compatible event loop on Windows.

Before serving:

1. Apply ingestion migrations through `0019_crv_kg_document_page` using the
   existing migration runner. This adds release-pinned, scope-filtered keyset
   pagination without changing the existing `find_documents` contract. Its grant
   flows through `crv_kg_reader`; keep the agent login execute-only.
2. Provision a **separate** `crv-agent-memory` database
   ([crv-agent-memory-db.bicep](infra/crv-agent-memory-db.bicep), database only on
   the existing server). As the migration identity run `crv-agent setup-memory`
   once, then `crv-agent grant-memory-runtime --login <runtime-login>`. The grant
   is DML-only on the agent/checkpoint tables and fails if the login can CREATE.
   Startup never runs DDL.
3. Use the dedicated runtime identity
   ([crv-research-agent-identity.bicep](infra/crv-research-agent-identity.bicep)):
   Search Index Data Reader and Cognitive Services OpenAI User only, federated to
   `crv/crv-research-agent`. Its PostgreSQL login comes from
   `crv-pipeline kg-grant-reader` (execute-only `crv_kg_reader`). PostgreSQL TLS is
   `verify-full`; `CRV_POSTGRES_ROOT_CERT` accepts `system` or an approved CA file.
4. `crv-agent check [--models]` verifies Search, the KG release, memory tables and
   least privilege (KG table SELECT and memory DDL must be denied); it exits
   non-zero on failure.
5. Run `crv-agent serve` for loopback development. The
   [backend Dockerfile](src/backend/Dockerfile) is available for internal packaging
   (`--build-arg PIP_INDEX_URL=...` for an approved mirror; TLS stays verified).

On the private AKS cluster,
[render-research-agent.ps1](infra/kubernetes/research-agent/render-research-agent.ps1)
renders one job at a time (`kg-login`, `memory-setup`, `memory-grant`,
`agent-check`, `retrieval-eval`) with digest-pinned images and applies it through
`az aks command invoke`. No Service or ingress is created.

The semantic ranker must be on the Standard billing plan: on the Free plan,
requests fail with a billing error once the monthly allowance is consumed.

The API exposes owned conversations, `/api/v1/chat`, feedback and health.
Chat requires UUID `thread_id` and `request_id` plus a message. SSE streams
progress, then a verified, persisted `answer`, then `done`; failures emit `error`,
never draft answer tokens. Reusing a request ID replays its recorded status/result.
Client disconnects cancel work. A lost pod's stale run is marked failed on a later
request rather than silently rerun. Conversation history survives restarts;
there is no cross-thread personal memory.

```powershell
python -m pytest .\src\backend\tests -q
python -m pytest .\src\backend\tests\integration -m integration -q
python -m ruff check .\src\backend
python -m mypy --config-file .\src\backend\pyproject.toml .\src\backend\crv_agent
# Run lint-imports from src\backend.
```

Integration tests start disposable local PostgreSQL containers. The answer
evaluator is `python -m crv_agent.evaluation.cli --help`; it requires
`--confirm-live` before calling a backend and an additional `--allow-heldout` for
the held-out split. `--judge` scores atomic fact recall; `--gate` fails on missing
or inadequate metrics. Missing expected sources/numeric tolerances are reported
as unmeasured, never passed. Proposed sources in the expert review sheet are not
ground truth until confirmed. Set server-side `CRV_RETRIEVAL_MODE=search` for the
Search-only ablation and `kg` for KG-steered retrieval; compare identical releases.
Progress latency and first **verified answer** latency are measured separately.
These tools do not establish scientific accuracy or production readiness by themselves.

Retrieval-depth experiments use `python -m crv_agent.evaluation.retrieval`. It emits
the exact context each arm would expose, without judging:

- `A0`/`A1` reproduce golden-eval `semantic`/`semantic_focused` at 10.
- Pack variants `P12`/`P16`/`P24`/`P16W2000`, caption-anchored `CAP` and LLM-reranked
  `LLM` are single-query arms.
- `DEC` is planner decomposition; `KGB` adds the precise KG boost.
- `CEIL` is the top-50 full-chunk ceiling.
- `AAR` is the Azure AI Search agentic-retrieval comparator: knowledge source and
  knowledge base in [infra/search](infra/search), minimal effort with an enforced
  release filter.

`python -m ingest_pipeline.tools.golden score` scores those contexts with the
golden-eval anchors and judge. The judge is repeated, with a majority vote per fact,
and every arm gets paired bootstrap confidence intervals. Tune on `dev`; the
held-out split needs `--allow-heldout` and is run once for the frozen winner. The
winning configuration becomes the runtime default through `CRV_EVIDENCE_PACK_SIZE`,
`CRV_EVIDENCE_EXCERPT_CHARACTERS`, `CRV_EVIDENCE_ANCHOR` and `CRV_EVIDENCE_RERANK`.

## Pilot acceptance checks

Start with a copied, small representative subset, not all 24 GiB:

1. Reconcile every source/archive member with its manifest outcome.
2. Check a historical scan, two-column article, table-heavy report, converted
   DOC/PPT, mislabeled file and the flora workbook.
3. Verify reading order, equations/captions, decimal qualifiers, footnotes,
   original pages/cells and converted-source citations manually.
4. Verify single-page byte-limit failures and incomplete-evidence handling.
5. Review collection/site/type labels and confirm LIMS commercial proposals
   cannot be mistaken for measured laboratory exports.
6. Compare layout/selective policies on extraction quality, retrieval relevance,
   cost, latency, memory and scratch usage before choosing the production policy.
7. Test authorization denial and permitted citation access before enabling serving.

Path-derived labels are heuristic. Titles currently use filenames; authors,
publication dates, projects and scientific topics are not automatically asserted.
No real LIMS connector, automatic scientific validation, or OCR accuracy guarantee
is included.

## Tests and retained compatibility

From `src\ingestion`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check ingest_pipeline tests office-converter
.\.venv\Scripts\python.exe -m mypy
.\.venv\Scripts\lint-imports.exe
```

The default suite excludes PostgreSQL integration and paid live-provider tests.
Docker/PostgreSQL integration, actual Office conversion and live Azure quality
checks must be run separately in an approved environment.

Removed copied standalone PFD/RDF knowledge exporters and the old `maint-content`
retrieval evaluator, their dedicated tests and unused RDF dependencies. The
durable engine, migrations, reusable parsers, discovery/bakeoff tools and
maintenance compatibility contracts remain: those are still used by existing
runtime or regression paths. The corpus and local virtual environments are not
deleted; generated Python state is ignored by Git.