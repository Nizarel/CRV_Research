#!/usr/bin/env python3
"""Verify the staged CRV sources on the DGX and link the backend's view (DGX side).

Reads ``manifests/tree.tsv``, ``manifests/dirs.tsv`` and ``manifests/sources.tsv`` written by
``stage_sources.py`` below ROOT. Checks that the tree holds exactly the laptop's folders and files
(same relative paths, sizes and SHA-256), that every derivative matches its content hash, then
creates ``crv/sources/<digest>.pdf`` as hard links (no extra space) for ``FilesystemSources``.
Idempotent; exits 1 on any mismatch and prints one JSON summary.

Usage:
    python3 link_sources.py /srv/crv-secure/sources
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ERROR = 2
DIGEST = re.compile(r"[0-9a-f]{64}")
CHUNK = 8 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def read_tsv(path: Path, columns: int) -> list[list[str]]:
    rows = [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines() if line]
    if any(len(row) != columns for row in rows):
        raise ValueError(f"{path} has a malformed line")
    return rows


def inside(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or relative.startswith("/"):
        raise ValueError(f"Path escapes the root: {relative!r}")
    return path


def run(root: Path) -> int:
    root = root.resolve()
    tree = read_tsv(root / "manifests" / "tree.tsv", 3)
    sources = read_tsv(root / "manifests" / "sources.tsv", 4)
    top = {row[2].split("/", 1)[0] for row in tree}
    expected = {row[2]: (row[0], int(row[1])) for row in tree}

    def check(relative: str) -> str | None:
        sha, size = expected[relative]
        path = inside(root, relative)
        if not path.is_file() or path.is_symlink():
            return f"missing: {relative}"
        if path.stat().st_size != size or sha256_file(path) != sha:
            return f"content differs: {relative}"
        return None

    with ThreadPoolExecutor(8) as pool:
        problems = [problem for problem in pool.map(check, expected) if problem]
    present = {
        (Path(directory) / name).relative_to(root).as_posix()
        for name_top in top
        for directory, _, names in os.walk(root / name_top)
        for name in names
    }
    extra = sorted(present - set(expected))
    problems += [f"unexpected: {relative}" for relative in extra[:20]]
    folders = root / "manifests" / "dirs.tsv"
    if folders.is_file():
        wanted = {line for line in folders.read_text(encoding="utf-8").splitlines() if line}
        found = {
            Path(directory).relative_to(root).as_posix()
            for name_top in top for directory, _, _ in os.walk(root / name_top)
        }
        problems += [f"missing folder: {relative}" for relative in sorted(wanted - found)[:20]]
        problems += [f"unexpected folder: {relative}" for relative in sorted(found - wanted)[:20]]

    links = root / "crv" / "sources"
    links.mkdir(parents=True, exist_ok=True)
    linked = derivatives = 0
    for digest, content_hash, size, relative in sources:
        if not DIGEST.fullmatch(digest) or not DIGEST.fullmatch(content_hash):
            problems.append(f"bad digest line: {digest}")
            continue
        source = inside(root, relative)
        if relative.startswith("derivatives/"):
            derivatives += 1
            if not source.is_file() or sha256_file(source) != content_hash:
                problems.append(f"derivative differs: {relative}")
                continue
        elif expected.get(relative, ("", 0))[0] != content_hash:
            problems.append(f"source not in tree: {relative}")
            continue
        if source.stat().st_size != int(size):
            problems.append(f"size differs from blob: {relative}")
            continue
        target = links / f"{digest}.pdf"
        if target.exists() and os.path.samefile(target, source):
            linked += 1
            continue
        temporary = links / f".{digest}.tmp"
        temporary.unlink(missing_ok=True)
        os.link(source, temporary)
        temporary.replace(target)
        linked += 1

    summary = {
        "root": str(root), "tree_files": len(expected), "tree_verified": len(expected) - sum(
            1 for problem in problems if problem.startswith(("missing", "content"))),
        "unexpected_files": len(extra), "cited_pdfs": len(sources), "linked": linked,
        "derivatives": derivatives, "problems": problems[:50], "ok": not problems,
    }
    print(json.dumps(summary))
    return EXIT_SUCCESS if not problems else EXIT_FAILURE


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return EXIT_ERROR
    try:
        return run(Path(sys.argv[1]))
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
