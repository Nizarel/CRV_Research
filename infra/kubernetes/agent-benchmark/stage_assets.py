"""In-job setup for the agent benchmark: copy the mounted files into a writable work dir and
download the content-addressed photo/drawing assets from blob storage, verifying each SHA-256.
``--pack`` prints the run's ``results-*.json`` files as one gzip+base64 line for ``kubectl logs``;
``--upload PREFIX`` stores them in the asset container instead (scheduled runs).

    python stage_assets.py --mounted /evaluation --work /work
    python stage_assets.py --work /work --pack
    python stage_assets.py --work /work --upload eval/results/nightly-20261015T1300
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import shutil
from pathlib import Path


def pack(work: Path) -> str:
    results = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(work.glob("results-*.json"))
    }
    payload = json.dumps(results, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(gzip.compress(payload)).decode("ascii")


def _container(name: str):
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient(
        f"https://{os.environ['AZURE_STORAGE_ACCOUNT']}.blob.core.windows.net",
        credential=DefaultAzureCredential(),
    )
    return service.get_container_client(name)


def upload(work: Path, container: object, prefix: str) -> list[str]:
    names = []
    for path in sorted(work.glob("results-*.json")):
        name = f"{prefix.strip('/')}/{path.name}"
        container.upload_blob(name, path.read_bytes(), overwrite=False)
        names.append(name)
    return names


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mounted", type=Path)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--pack", action="store_true")
    parser.add_argument("--upload", metavar="PREFIX")
    args = parser.parse_args()
    if args.pack:
        print(pack(args.work))
        return
    if args.upload:
        manifest = json.loads((args.work / "assets.json").read_text(encoding="utf-8"))
        names = upload(args.work, _container(manifest["container"] or "images"), args.upload)
        print(json.dumps({"uploaded": names}))
        return

    for item in args.mounted.iterdir():
        if item.is_file():
            shutil.copyfile(item, args.work / item.name)
    manifest = json.loads((args.mounted / "assets.json").read_text(encoding="utf-8"))
    if not manifest["files"]:
        return

    container = _container(manifest["container"])
    for relative, entry in manifest["files"].items():
        data = container.download_blob(entry["blob"]).readall()
        digest = hashlib.sha256(data).hexdigest()
        if digest != entry["sha256"]:
            raise SystemExit(f"Hash mismatch for {relative}: {digest} != {entry['sha256']}")
        target = args.work / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(json.dumps({"staged_assets": len(manifest["files"])}))


if __name__ == "__main__":
    main()
