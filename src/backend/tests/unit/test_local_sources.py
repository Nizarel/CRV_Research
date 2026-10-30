import os
from pathlib import Path

import pytest

from crv_agent.adapters import local_sources
from crv_agent.adapters.local_sources import FilesystemSources
from crv_agent.ports.sources import SourceNotFound
from tests.unit import test_sources_api as api

DIGEST = "a" * 64
PDF = b"%PDF-1.7 " + bytes(range(256)) * 4


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "secure" / "crv" / "sources"
    folder.mkdir(parents=True)
    (folder / f"{DIGEST}.pdf").write_bytes(PDF)
    return tmp_path / "secure"


async def collect(sources: FilesystemSources, digest: str, start: int, end: int) -> bytes:
    return b"".join([chunk async for chunk in sources.read(digest, start, end)])


@pytest.mark.asyncio
async def test_stat_and_ranges(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_sources, "CHUNK", 7)
    sources = FilesystemSources(root)
    info = await sources.stat(DIGEST)
    assert info.size == len(PDF) and info.etag == f"{DIGEST}-{len(PDF):x}"
    assert await collect(sources, DIGEST, 0, len(PDF)) == PDF
    assert await collect(sources, DIGEST, 2, 30) == PDF[2:30]
    assert await collect(sources, DIGEST, len(PDF) - 3, len(PDF) + 10) == PDF[-3:]


@pytest.mark.asyncio
@pytest.mark.parametrize("digest", [
    "b" * 64, "A" * 64, "a" * 63, "../" + "a" * 61, "a" * 64 + "/..", "",
])
async def test_unknown_or_malformed_digests_are_not_found(root: Path, digest: str) -> None:
    sources = FilesystemSources(root)
    with pytest.raises(SourceNotFound):
        await sources.stat(digest)
    with pytest.raises(SourceNotFound):
        await collect(sources, digest, 0, 1)


@pytest.mark.asyncio
async def test_symlink_outside_root_is_refused(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"secret")
    link = root / "crv" / "sources" / f"{'c' * 64}.pdf"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("Symlinks are not permitted here")
    with pytest.raises(SourceNotFound):
        await FilesystemSources(root).stat("c" * 64)


def test_missing_root_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        FilesystemSources(tmp_path / "missing")


def test_api_serves_local_sources(root: Path) -> None:
    client = api.client(FilesystemSources(root))  # type: ignore[arg-type]
    response = client.get(f"/api/v1/sources/{DIGEST}.pdf", headers={"Range": "bytes=2-9"})
    assert response.status_code == 206 and response.content == PDF[2:10]
    assert client.get(f"/api/v1/sources/{'b' * 64}.pdf").status_code == 404
