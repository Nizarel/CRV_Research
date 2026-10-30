"""Upload a finished db-export directory to Blob Storage (main container of the db-export Job).

Uploads every file listed in SHA256SUMS, then SHA256SUMS itself, to CONTAINER under
RUN_ID/DATABASE/. Each file is hashed again before upload, sent with per-block MD5 validation and
never overwrites an existing blob. Prints a JSON summary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from azure.identity import WorkloadIdentityCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

_LINE = re.compile(r"^(?P<sha256>[0-9a-f]{64}) [ *](?P<name>[A-Za-z0-9][A-Za-z0-9._-]{0,200})$")
_BLOCK = 8 * 1024 * 1024


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(_BLOCK):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _listed(sums: Path) -> dict[str, str]:
    listed: dict[str, str] = {}
    for line in sums.read_text("utf-8").splitlines():
        match = _LINE.match(line)
        if match is None or match["name"] in listed or match["name"] == "SHA256SUMS":
            raise SystemExit(f"Unexpected SHA256SUMS line: {line!r}")
        listed[match["name"]] = match["sha256"]
    if not listed:
        raise SystemExit("SHA256SUMS lists no files")
    return listed


def main() -> None:
    run_id = os.environ["RUN_ID"]
    database = os.environ["DATABASE"]
    root = Path(os.environ.get("OUT_ROOT", "/work/out")) / database
    if not (root / "DONE").is_file():
        raise SystemExit(f"{root} has no DONE marker: the export did not finish")
    sums = root / "SHA256SUMS"
    listed = _listed(sums)
    prefix = f"{run_id}/{database}/"
    service = BlobServiceClient(os.environ["ACCOUNT_URL"], credential=WorkloadIdentityCredential())
    container = service.get_container_client(os.environ["CONTAINER"])
    uploaded = []
    for name in [*sorted(listed), "SHA256SUMS"]:
        path = root / name
        sha256, size = _sha256(path)
        if name in listed and sha256 != listed[name]:
            raise SystemExit(f"{name} changed after SHA256SUMS was written")
        blob = container.get_blob_client(prefix + name)
        with path.open("rb") as handle:
            blob.upload_blob(
                handle, length=size, overwrite=False, validate_content=True, max_concurrency=4,
                metadata={"sha256": sha256},
                content_settings=ContentSettings(content_type="application/octet-stream"),
            )
        stored = blob.get_blob_properties()
        if stored.size != size or stored.metadata.get("sha256") != sha256:
            raise SystemExit(f"{name}: stored blob does not match the local file")
        uploaded.append({"name": name, "bytes": size, "sha256": sha256})
        print(json.dumps({"uploaded": prefix + name, "bytes": size}), flush=True)
    print(json.dumps({"container": container.container_name, "prefix": prefix,
                      "files": uploaded}))


if __name__ == "__main__":
    main()
