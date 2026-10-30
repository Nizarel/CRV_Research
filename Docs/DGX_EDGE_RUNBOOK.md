---
title: CRV Edge Runbook for dgx-niz01 and dgx-niz02
description: Step-by-step procedure to build the ARM64 images, create edge logins and memory, re-embed the corpus with BGE-M3, start the edge stack, and check it, after the staging restore passes.
author: CRV Research
ms.date: 2026-10-29
ms.topic: how-to
keywords:
  - dgx spark
  - edge
  - runbook
  - docker compose
  - bge-m3
estimated_reading_time: 15
---

## Before you start

Run these steps on dgx-niz01 in an interactive SSH session, from the repository root, after the
[preflight](./DGX_MIGRATION_PREFLIGHT.md) restore and its P6 checks pass. The steps have not been
run on the DGX yet; record each command's output in `/srv/crv-secure/restore-logs` as you go.

> [!IMPORTANT]
> Keep every CRV byte on `/srv/crv-secure`. Model weights and images live elsewhere. Never run the
> staging and edge PostgreSQL stacks at the same time: both use `/srv/crv-secure/pgdata`.

You need:

* The LUKS volume open (`sudo bash infra/dgx/staging/luks-volume.sh open`).
* The restored `ingestion-catalog` that passed `restore.sh` and `verify-staging.sh`.
* Docker with the NVIDIA runtime on dgx-niz01, outbound HTTPS to `integrate.api.nvidia.com`, and
  the NVIDIA API key.

Placement, memory budgets and model choices are in [DGX_EDGE_PLACEMENT.md](./DGX_EDGE_PLACEMENT.md).

## 1. Build the ARM64 images

Build natively with host networking (bridge builds intermittently failed to reach PyPI on
dgx-niz01). Tag each image with the Git revision and record its digest.

```bash
rev="$(git rev-parse --short HEAD)"
docker build --network host -t "crv-backend:edge-$rev" src/backend
docker build --network host --build-arg VITE_BASE_PATH=/ -t "crv-web:edge-$rev" src/web/Crv-frontend
docker build --network host --target runtime -t "crv-pipeline:edge-$rev" src/ingestion
docker image inspect --format '{{.Id}} {{.Architecture}}' \
    "crv-backend:edge-$rev" "crv-web:edge-$rev" "crv-pipeline:edge-$rev"
```

Smoke-test the backend image before going further:

```bash
docker run --rm "crv-backend:edge-$rev" python -c \
    "import platform, psycopg; print(platform.machine(), psycopg.pq.__impl__, psycopg.pq.version())"
```

Expect `aarch64` and a working `libpq` implementation.

## 2. Prepare the encrypted volume

Volumes created before 2026-10-28 have no `sources` folder, and `/srv/crv-secure` belongs to root
(skip `sources` here if you move the staged copy into place as described in step 2a):

```bash
sudo install -d -m 0700 -o "$(id -u)" -g "$(id -g)" /srv/crv-secure/sources /srv/crv-secure/eval
install -m 0600 infra/dgx/edge/edge.env.example /srv/crv-secure/secrets/edge.env
```

Edit `/srv/crv-secure/secrets/edge.env`: the KG release (`crvkg-20261021-adb13a21c7da`, as on
AKS) and, if they change, the served model names. It holds no secrets but lives in `secrets/`
because the volume root is owned by root. Copy the repository to the DGX with LF line endings:
shell scripts with CRLF fail on `set -o pipefail`.

Put the NVIDIA API key in `/srv/crv-secure/secrets/nvidia-api.key` without it touching a command
line or shell history; for example, from the operator laptop where it is stored with DPAPI:

```powershell
$k = (Get-Content "$env:USERPROFILE\.crv\nvidia-api-key.dpapi" -Raw).Trim() | ConvertTo-SecureString
[Net.NetworkCredential]::new('', $k).Password | ssh dgx-niz01 `
    'umask 077 && cat > /srv/crv-secure/secrets/nvidia-api.key'
```

Export the variables the Compose file needs:

```bash
export CRV_SECURE=/srv/crv-secure CRV_UID="$(id -u)" CRV_GID="$(id -g)"
export CRV_PG_IMAGE=crv-postgres16:age1.6.0-pgvector0.8.2
export CRV_BACKEND_IMAGE="crv-backend:edge-$rev" CRV_WEB_IMAGE="crv-web:edge-$rev"
edge() { docker compose -f infra/dgx/edge/compose.yaml "$@"; }
```

`CRV_NIZ02_ADDRESS` defaults to 192.168.88.8 (dgx-niz02 on Wi-Fi); it matters only in phase 2. Set
it to 192.168.100.11, the direct link where the dgx-niz02 model server listens.

## 2a. Copy the source PDFs

The evidence cites each source PDF as `crv/sources/<sha256(source_path)>.pdf`. The laptop copy of
the corpus keeps the original folder structure, but Windows replaced characters such as `:` in
folder names, so files are matched by content hash, not by name. Most cited PDFs exist on the
laptop; the rest are derivatives (PDFs converted from DOC and PPT, and archive members), which come
from the `crv-sources` container byte for byte.

On the laptop, from the repository root (staging stays on the BitLocker disk):

```powershell
$stage = "$env:TEMP\crv-migration\sources\stage"
src\backend\.venv\Scripts\python.exe infra\dgx\edge\stage_sources.py plan `
    --corpus-parent Data --tree 'DATAROOM CRV' --staging $stage `
    --account-url https://stawmc6icu2kueq.blob.core.windows.net
src\backend\.venv\Scripts\python.exe infra\dgx\edge\stage_sources.py send `
    --corpus-parent Data --tree 'DATAROOM CRV' --staging $stage `
    --ssh 'ssh dgx-niz01' --dest /srv/crv-secure/sources
Get-Content infra\dgx\edge\link_sources.py -Raw | ssh dgx-niz01 'python3 - /srv/crv-secure/sources'
```

[stage_sources.py](../infra/dgx/edge/stage_sources.py) streams the tree unchanged as a UTF-8 tar
over SSH (long and accented names, and empty folders, included), with the derivatives and three
manifests (`tree.tsv`, `dirs.tsv`, `sources.tsv`).
[link_sources.py](../infra/dgx/edge/link_sources.py) then checks that the DGX tree holds exactly
the laptop's folders and files (same relative paths, sizes and SHA-256), and creates the
`crv/sources/<digest>.pdf` hard links the backend serves. It must report `"ok": true`. Delete the
laptop staging folder afterwards.

> [!NOTE]
> On 2026-10-28 the copy went to `/srv/crv-secure/bundles/sources-staging` because the volume had no
> `sources` folder and creating one needs `sudo`. It verified clean (72 folders, 1,967 files, 2,023
> cited PDFs including 52 derivative files) and was moved to `/srv/crv-secure/sources` the same day,
> where it verified clean again on 2026-10-29. To repeat that on a new volume, move it into place
> once instead of creating `sources` in step 2, then re-run the check; hard links survive the move:
>
> ```bash
> sudo mv /srv/crv-secure/bundles/sources-staging /srv/crv-secure/sources
> python3 infra/dgx/edge/link_sources.py /srv/crv-secure/sources
> ```

If the `dgx-niz01` name does not resolve (it uses mDNS), add `-o HostName=192.168.88.10` to the
SSH commands.

## 3. Switch PostgreSQL to the edge stack

```bash
docker compose -f infra/dgx/staging/compose.yaml down
edge up -d postgres web
edge ps
```

The web container starts without the backend; its API routes answer 502 until step 6.

## 4. Create the edge logins and the memory database

```bash
CRV_PG_CONTAINER=crv-edge-postgres-1 bash infra/dgx/edge/edge-logins.sh
```

[edge-logins.sh](../infra/dgx/edge/edge-logins.sh) generates
`secrets/crv_edge_agent.password` and `secrets/crv_edge_migrator.password` (0600, never printed)
and runs [edge-logins.sql](../infra/sql/migration/edge-logins.sql). It creates:

* `crv_edge_agent`: the backend login. It may `SET ROLE crv_kg_reader` for KG and retrieval
  reads, inherits nothing else, and gets DML on the memory tables in the next step.
* `crv_edge_migrator`: owner of the empty `crv-agent-memory` database.

It refuses to continue if either login can create objects in `ingestion-catalog` or is a member of
its owner role. The script is idempotent; a second run keeps both passwords.

Create the memory schema and grant the runtime login, as the migrator:

```bash
migrator=(-e CRV_MEMORY_USER=crv_edge_migrator
          -e CRV_MEMORY_PASSWORD_FILE=/run/crv-secrets/crv_edge_migrator.password)
edge run --rm --no-deps "${migrator[@]}" backend crv-agent setup-memory
edge run --rm --no-deps "${migrator[@]}" backend \
    crv-agent grant-memory-runtime --login crv_edge_agent
```

`grant-memory-runtime` fails if the runtime login could still create objects.

## 5. Re-embed the corpus with BGE-M3

Start the BGE-M3 server as a container named `crv-bge-m3` on the `crv-edge_models` network, from
the NVIDIA vLLM image for DGX Spark, serving `BAAI/bge-m3` as an embedding model on port 8000
under the served name `bge-m3`. Set `--gpu-memory-utilization` explicitly (about 0.05) so it does
not reserve most of the unified memory. Record the image digest and model revision.

For the backfill only, connect it to the internal database network so the pipeline running beside
PostgreSQL reaches it:

```bash
docker network connect crv-edge_database crv-bge-m3
pipeline() {
    docker run --rm --network container:crv-edge-postgres-1 \
        -e TIZ_DB__DSN=postgresql://postgres@127.0.0.1:5432/ingestion-catalog "$@"
}
pipeline "crv-pipeline:edge-$rev" retrieval-embed --release crv-v1 --dry-run
pipeline -e TIZ_LOCAL_EMBEDDINGS__URL=http://crv-bge-m3:8000/v1 \
    -e TIZ_LOCAL_EMBEDDINGS__SERVED_MODEL=bge-m3 \
    -e 'TIZ_LOCAL_EMBEDDINGS__PRIVATE_HTTP_HOSTS=["crv-bge-m3"]' \
    "crv-pipeline:edge-$rev" retrieval-embed --release crv-v1 --limit 500
```

Check the 500-row report (`written`, `failed`, `seconds`), then run without `--limit`. The command
is resumable: rerun it after any interruption. It embeds the stored chunk text, caches vectors
under `BAAI/bge-m3` in `ingest.embedding`, and only writes rows whose text is unchanged. A
non-zero `failed` count lists rows the model rejected (for example, text longer than its context);
they keep a null vector and the command exits 1.

Build the M3 index and verify it:

```bash
pipeline "crv-pipeline:edge-$rev" retrieval-index --model m3 --maintenance-work-mem 1536MB \
    --parallel-workers 4
pipeline "crv-pipeline:edge-$rev" retrieval-verify recall explain --model m3 --release crv-v1 \
    --sample 50
docker network disconnect crv-edge_database crv-bge-m3
```

`release_status` must report `m3` equal to `chunks`, and ANN recall at least 0.95. The backend
refuses to start while any chunk of the release lacks an M3 vector, so resolve every reported
failure first (for example, raise the embedding server's maximum input length) and rerun
`retrieval-embed`. Keep `--maintenance-work-mem` below the container's 2 GB `shm_size`: a
parallel index build allocates it in shared memory and fails with "No space left on device".

Measured on 2026-10-28 (vLLM 26.09, `BAAI/bge-m3` revision `5617a9f`, 8% GPU memory): the
backfill embedded 126,690 texts and reused 14,930 cached ones in 649 seconds with no failures;
the index built in 34 seconds (1,010 MB); recall against exact search was 0.9996 (minimum 0.98)
with p95 of 17 ms.

> [!NOTE]
> The `postgres@127.0.0.1` connection uses the image's local trust rule inside the PostgreSQL
> container's network namespace, as `verify-staging.sh` does. Nothing outside that namespace can
> use it.

## 6. Start the models and the backend

1. Optional: start `crv-reranker` on the `crv-edge_models` network (`BAAI/bge-reranker-v2-m3` as a
   score model on port 8000, served name `bge-reranker-v2-m3`, explicit
   `--gpu-memory-utilization`), then switch the reranker lines in `edge.env` to `vllm`.
2. Phase 1 generation uses the NVIDIA API with the key from step 2; nothing runs on dgx-niz02. In
   phase 2, start the System 1 and System 2 servers on dgx-niz02 bound to the private interface,
   with `--api-key` from a 0600 file, allow only dgx-niz01 in its firewall, and point
   `CRV_MODEL_ENDPOINT` at them.
3. Probe each model role, then start the backend:

```bash
edge run --rm --no-deps backend crv-agent check --models
edge up -d
edge ps
curl -fsS http://127.0.0.1:8080/api/ready
```

`check --models` sends one strict structured-output call per role with that role's schema and
reports the served model, latency and parse result. Fix any failed role before step 7.

## 7. Validate

Follow [DGX_EDGE_VALIDATION.md](./DGX_EDGE_VALIDATION.md): retrieval arms, paired scoring,
end-to-end answers, latency, then the Azure-independence test.

## Stop and restart

```bash
edge down
sudo bash infra/dgx/staging/luks-volume.sh close
```

After a reboot, open the volume, export the variables from step 2 and run `edge up -d`. Nothing
restarts by itself while the volume is locked.

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Backend exits with `PostgreSQL password auth needs a password file` | `CRV_SECURE` and the bind mount of `secrets/`; file names from step 4 |
| `Cannot read the PostgreSQL password file` | The backend runs as `CRV_UID`; the file must be owned by that user with mode 0600 |
| `Model endpoint must be an HTTPS origin` | Plain HTTP needs the host in `CRV_PRIVATE_HTTP_HOSTS` and model auth other than Entra |
| `chunk(s) lack m3 vectors` at startup | Step 5 is incomplete, or rows failed; rerun `retrieval-embed` |
| `Embedding response has an unexpected shape` | The embedding server returns a dimension other than 1,024, or a different model |
| Web returns 502 on `/api/v1` | `edge logs backend`; the backend must be healthy and share the web network namespace |
| dgx-niz01 restarted on its own (`last -x` shows a boot with no shutdown) | See [Unclean restarts](#unclean-restarts) |

### Unclean restarts

Between 2026-10-28 and 2026-10-29 dgx-niz01 restarted four times without a shutdown sequence
(boots ending 10-28 08:01, 09:57, 11:33 and 10-29 20:35). The journal of each boot stops
mid-activity, with no panic, watchdog, thermal or GPU (Xid) message, and pstore is empty. Automatic
updates are not the cause: `unattended-upgrades` is not installed.

* Two resets hit both hosts at once: dgx-niz02 also restarted without a shutdown at 10-28 09:58
  (the same second as dgx-niz01) and 10-29 20:38. A shared power event (outlet, power strip or
  circuit) explains these.
* The other two hit dgx-niz01 only, 2 to 4 seconds after a USB-C device failed to enumerate on bus 6
  port 1 (`device not accepting address, error -71`, then `attempt power cycle`); a USB Ethernet
  adapter (`r8152`) appeared on that port before the 11:33 reset.

DGX Spark draws power over USB-C. Put both hosts on a UPS or separate circuits, use the supplied
power adapter and cable on the power port, seat it firmly, and keep USB-C dongles and hubs off the
adjacent ports. To capture evidence of the next one:

```bash
last -x -F reboot shutdown | head          # a boot with no shutdown line is unclean
journalctl --list-boots
journalctl -b -1 -n 50 --no-pager          # last messages of the previous boot
journalctl -b -1 -k -p warning --no-pager  # kernel warnings, including USB errors
sudo ls /sys/fs/pstore                     # a kernel panic record, if any
```

After any restart the stack stays down until the volume is opened again (step 2).
