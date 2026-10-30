#!/usr/bin/env python3
"""Stage the CRV source PDFs from the operator laptop for the DGX edge (laptop side).

``plan`` hashes the local corpus tree, lists the ``crv/sources/<sha256(source_path)>.pdf``
blobs the evidence cites, matches each blob to a local file by content hash, and downloads only
the derivatives that do not exist locally (PDFs converted from DOC/PPT, archive members). It writes
``manifests/tree.tsv`` (every local file) and ``manifests/sources.tsv`` (digest to file).

``send`` streams the unchanged tree, the derivatives and the manifests as one UTF-8 PAX tar over
SSH into a directory on the DGX encrypted volume. ``link_sources.py`` then verifies every file
and creates the ``crv/sources/<digest>.pdf`` hard links the backend serves.

Usage:
    python stage_sources.py plan --corpus-parent Data --tree "DATAROOM CRV" \
        --account-url https://<account>.blob.core.windows.net --staging <dir>
    python stage_sources.py send --corpus-parent Data --tree "DATAROOM CRV" --staging <dir> \
        --ssh "ssh -o HostName=<ip> dgx-niz01" --dest /srv/crv-secure/bundles/sources-staging
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tarfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ERROR = 2
DIGEST = re.compile(r"[0-9a-f]{64}")
BLOB_NAME = re.compile(r"crv/sources/([0-9a-f]{64})\.pdf")
REMOTE_DEST = re.compile(r"/srv/crv-secure/[A-Za-z0-9._/-]+")
CHUNK = 8 << 20

logger = logging.getLogger(__name__)


def long_path(path: Path) -> Path:
    """Windows paths longer than 260 characters need the extended-length prefix."""
    text = str(path.resolve())
    if os.name == "nt" and not text.startswith("\\\\?\\"):
        return Path("\\\\?\\" + text)
    return path.resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def tree_files(parent: Path, tree: str) -> Iterator[tuple[Path, str]]:
    """Every regular file below ``parent/tree`` with its POSIX path relative to ``parent``."""
    root = long_path(parent / tree)
    base = long_path(parent)
    for directory, _, names in os.walk(root):
        for name in sorted(names):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Not a regular file: {path}")
            relative = path.relative_to(base).as_posix()
            if any(char in relative for char in "\t\r\n"):
                raise ValueError(f"Control character in path: {relative!r}")
            yield path, relative


def tree_dirs(parent: Path, tree: str) -> list[str]:
    """Every directory below ``parent/tree`` (empty ones too), POSIX and relative to ``parent``."""
    base = long_path(parent)
    return sorted(
        Path(directory).relative_to(base).as_posix()
        for directory, _, _ in os.walk(long_path(parent / tree))
    )


def hash_tree(parent: Path, tree: str) -> list[tuple[str, int, str]]:
    files = list(tree_files(parent, tree))

    def one(item: tuple[Path, str]) -> tuple[str, int, str]:
        path, relative = item
        return sha256_file(path), path.stat().st_size, relative

    with ThreadPoolExecutor(8) as pool:
        return sorted(pool.map(one, files), key=lambda row: row[2])


def source_blobs(account_url: str, container: str) -> list[dict[str, object]]:
    """Cited source PDFs: blob digest, content hash and size (metadata only)."""
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import ContainerClient

    client = ContainerClient(account_url, container, credential=DefaultAzureCredential())
    blobs = []
    for blob in client.list_blobs(name_starts_with="crv/sources/", include=["metadata"]):
        match = BLOB_NAME.fullmatch(blob.name)
        content_hash = (blob.metadata or {}).get("content_hash", "")
        if match is None:
            continue
        if not DIGEST.fullmatch(content_hash):
            raise ValueError(f"Blob without a content hash: {blob.name}")
        blobs.append({"name": blob.name, "digest": match[1], "content_hash": content_hash,
                      "size": blob.size})
    return blobs


def download(account_url: str, container: str, name: str, target: Path, expected: str) -> None:
    from azure.identity import DefaultAzureCredential
    from azure.storage.blob import BlobClient

    client = BlobClient(account_url, container, name, credential=DefaultAzureCredential())
    partial = target.with_suffix(".partial")
    with partial.open("wb") as handle:
        client.download_blob().readinto(handle)
    if sha256_file(partial) != expected:
        partial.unlink()
        raise ValueError(f"Downloaded {name} does not match its content hash")
    partial.replace(target)


def run_plan(args: argparse.Namespace) -> int:
    staging: Path = args.staging
    (staging / "manifests").mkdir(parents=True, exist_ok=True)
    (staging / "derivatives").mkdir(exist_ok=True)
    tree = hash_tree(args.corpus_parent, args.tree)
    local = {sha: relative for sha, _, relative in tree}
    blobs = source_blobs(args.account_url, args.container)
    rows, missing = [], {}
    for blob in sorted(blobs, key=lambda item: str(item["digest"])):
        content_hash = str(blob["content_hash"])
        if content_hash in local:
            path = local[content_hash]
        else:
            path = f"derivatives/{content_hash}.pdf"
            missing.setdefault(content_hash, str(blob["name"]))
        rows.append(f"{blob['digest']}\t{content_hash}\t{blob['size']}\t{path}")
    for content_hash, name in sorted(missing.items()):
        target = staging / "derivatives" / f"{content_hash}.pdf"
        if not target.exists() or sha256_file(target) != content_hash:
            download(args.account_url, args.container, name, target, content_hash)
    (staging / "manifests" / "tree.tsv").write_text(
        "".join(f"{sha}\t{size}\t{relative}\n" for sha, size, relative in tree), encoding="utf-8",
    )
    (staging / "manifests" / "dirs.tsv").write_text(
        "".join(f"{relative}\n" for relative in tree_dirs(args.corpus_parent, args.tree)),
        encoding="utf-8",
    )
    (staging / "manifests" / "sources.tsv").write_text(
        "".join(row + "\n" for row in rows), encoding="utf-8",
    )
    derived = sum(1 for row in rows if row.split("\t")[3].startswith("derivatives/"))
    summary = {
        "tree_files": len(tree), "tree_bytes": sum(size for _, size, _ in tree),
        "cited_pdfs": len(rows), "from_tree": len(rows) - derived,
        "from_derivatives": derived, "derivatives_downloaded": len(missing),
    }
    print(json.dumps(summary))
    return EXIT_SUCCESS


def run_send(args: argparse.Namespace) -> int:
    if not REMOTE_DEST.fullmatch(args.dest) or ".." in args.dest.split("/"):
        logger.error("The destination must be a plain path under /srv/crv-secure")
        return EXIT_ERROR
    staging: Path = args.staging
    manifests = staging / "manifests"
    if not all((manifests / name).is_file() for name in ("tree.tsv", "dirs.tsv", "sources.tsv")):
        logger.error("Run plan first")
        return EXIT_ERROR
    remote = (f"umask 077 && mkdir -p {shlex.quote(args.dest)} && "
              f"tar -x --no-same-owner -f - -C {shlex.quote(args.dest)}")
    command = [*shlex.split(args.ssh, posix=os.name != "nt"), "-o", "BatchMode=yes", remote]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert process.stdin is not None
    sent = 0

    def add(path: Path, arcname: str) -> None:
        nonlocal sent
        info = tarfile.TarInfo(arcname)
        stat = path.stat()
        info.size, info.mtime, info.mode = stat.st_size, int(stat.st_mtime), 0o600
        with path.open("rb") as handle:
            archive.addfile(info, handle)
        sent += stat.st_size

    with tarfile.open(fileobj=process.stdin, mode="w|", format=tarfile.PAX_FORMAT,
                      encoding="utf-8") as archive:
        # Directories first, so empty folders keep the laptop's structure too.
        for relative in tree_dirs(args.corpus_parent, args.tree):
            folder = tarfile.TarInfo(relative)
            folder.type, folder.mode = tarfile.DIRTYPE, 0o700
            archive.addfile(folder)
        for path, relative in tree_files(args.corpus_parent, args.tree):
            add(path, relative)
        for path in sorted((staging / "derivatives").glob("*.pdf")):
            add(path, f"derivatives/{path.name}")
        for name in ("tree.tsv", "dirs.tsv", "sources.tsv"):
            add(manifests / name, f"manifests/{name}")
    process.stdin.close()
    code = process.wait()
    print(json.dumps({"sent_bytes": sent, "ssh_exit": code}))
    return EXIT_SUCCESS if code == 0 else EXIT_FAILURE


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "send"):
        command = commands.add_parser(name)
        command.add_argument("--corpus-parent", type=Path, required=True)
        command.add_argument("--tree", required=True, help="Top folder below the corpus parent")
        command.add_argument("--staging", type=Path, required=True)
    plan = commands.choices["plan"]
    plan.add_argument("--account-url", required=True)
    plan.add_argument("--container", default="crv-sources")
    send = commands.choices["send"]
    send.add_argument("--ssh", required=True, help='SSH command, e.g. "ssh dgx-niz01"')
    send.add_argument("--dest", required=True, help="Directory under /srv/crv-secure")
    return parser


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    args = create_parser().parse_args()
    try:
        return run_plan(args) if args.command == "plan" else run_send(args)
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        return 130
    except (OSError, ValueError) as error:
        logger.error("%s", error)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
