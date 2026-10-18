from __future__ import annotations

import hashlib
import io
import os
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest
from fixture_builders import PdfPage, Sheet, pdf_bytes, xlsx_bytes

from ingest_pipeline.adapters.local_intake import CorpusIntake, long_path, verify_object
from ingest_pipeline.adapters.parsers.office import OfficeConverter
from ingest_pipeline.domain.errors import ConfigurationError, UnsupportedContentError
from ingest_pipeline.domain.intake import IntakeLimits, IntakeManifest


def archive_bytes(entries: dict[str, bytes]) -> bytes:
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return buffer.getvalue()


def test_intake_sniffs_bytes_expands_archives_and_keeps_originals(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    pdf = pdf_bytes([PdfPage(lines=["Report CR/AF/RP/01/21"])])
    workbook = xlsx_bytes([Sheet("Flora", {"B2": "Tri-K", "B3": "Species"})])
    (root / "misnamed.docx").write_bytes(pdf)
    (root / "copy.pdf").write_bytes(pdf)
    (root / "README.md").write_text("  ")
    (root / "bundle.zip").write_bytes(archive_bytes({"flora.xlsx": workbook, "copy.pdf": pdf}))
    output = tmp_path / "intake"
    report = CorpusIntake(output, IntakeLimits()).prepare(root)
    assert report.complete
    assert report.failed == 0
    assert len(report.items) == 6
    by_name = {item.source_path: item for item in report.items}
    assert by_name["misnamed.docx"].file_kind == "pdf"
    assert by_name["README.md"].status == "excluded"
    child = by_name["bundle.zip!/flora.xlsx"]
    assert child.archive_members == ("flora.xlsx",)
    assert child.ancestor_hashes == (by_name["bundle.zip"].input_hash,)
    assert len({by_name[n].content_hash for n in (
        "misnamed.docx", "copy.pdf", "bundle.zip!/copy.pdf"
    )}) == 1
    for item in report.items:
        if item.content_hash:
            verify_object(output, item.content_hash)
    assert IntakeManifest.model_validate_json((output / "manifest.json").read_bytes()) == report


@pytest.mark.parametrize("name", ["../escape.pdf", "/absolute.pdf", "C:\\escape.pdf"])
def test_unsafe_archive_paths_fail_explicitly(tmp_path: Path, name: str) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "bad.zip").write_bytes(archive_bytes({name: b"not written outside intake"}))
    report = CorpusIntake(tmp_path / "out", IntakeLimits()).prepare(root)
    assert report.complete
    assert report.failed == 1
    assert "unsafe" in (report.items[0].reason or "").lower()
    assert not (tmp_path / "escape.pdf").exists()


def test_symlink_archive_member_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    with zipfile.ZipFile(root / "bad.zip", "w") as archive:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../outside")
    report = CorpusIntake(tmp_path / "out", IntakeLimits()).prepare(root)
    assert report.failed == 1


def test_archive_depth_and_global_members_are_bounded(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "nested.zip").write_bytes(archive_bytes({
        "nested.zip": archive_bytes({"a.txt": b"one"}),
    }))
    report = CorpusIntake(
        tmp_path / "out", IntakeLimits(max_depth=1, max_ratio=1000)
    ).prepare(root)
    assert report.failed == 1
    assert "nesting" in (report.items[-1].reason or "")


def test_size_limits_and_legacy_failure_are_not_silent(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "large.txt").write_bytes(b"x" * 50)
    (root / "old.doc").write_bytes(bytes.fromhex("d0cf11e0a1b11ae1"))
    report = CorpusIntake(tmp_path / "out", IntakeLimits(max_source_bytes=20)).prepare(root)
    assert report.failed == 2
    assert any("Office" in (item.reason or "") for item in report.items)
    assert any("byte limit" in (item.reason or "") for item in report.items)


@pytest.mark.skipif(os.name != "nt", reason="Windows path handling")
def test_long_paths_are_not_omitted(tmp_path: Path) -> None:
    root = tmp_path / "source"
    folder = long_path(root / ("a" * 100) / ("b" * 100) / ("c" * 100))
    folder.mkdir(parents=True)
    (folder / "report.txt").write_bytes(b"Long path report")
    report = CorpusIntake(tmp_path / "out", IntakeLimits()).prepare(root)
    assert len(report.items) == 1
    assert report.items[0].status == "ready"


def test_output_inside_corpus_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside"):
        CorpusIntake(tmp_path / "out", IntakeLimits()).prepare(tmp_path)


def test_office_converter_requires_pinned_image(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="pinned"):
        OfficeConverter("example:latest").convert(tmp_path / "x.doc", tmp_path / "x.pdf", ".doc")


def test_office_conversion_is_isolated_and_cached(tmp_path: Path, monkeypatch) -> None:
    calls = []
    pdf = pdf_bytes([PdfPage(lines=["converted report"])])

    def run(command, **kwargs):
        calls.append(command)
        mount = next(v for v in command if v.endswith("dst=/output"))
        target = mount.split("src=", 1)[1].split(",dst=", 1)[0]
        (Path(target) / "source.pdf").write_bytes(pdf)
        assert kwargs["timeout"] == 120
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("shutil.which", lambda _: "docker")
    monkeypatch.setattr("subprocess.run", run)
    root = tmp_path / "source"
    root.mkdir()
    data = bytes.fromhex("d0cf11e0a1b11ae1") + b"legacy"
    (root / "report.doc").write_bytes(data)
    converter = OfficeConverter("local/office@sha256:" + "a" * 64)
    intake = CorpusIntake(tmp_path / "out", IntakeLimits(), converter)
    first = intake.prepare(root)
    second = intake.prepare(root)
    assert first == second
    assert len(calls) == 1
    item = first.items[0]
    assert item.status == "converted"
    assert item.input_hash == hashlib.sha256(data).hexdigest()
    assert item.content_hash == hashlib.sha256(pdf).hexdigest()
    assert "--network=none" in calls[0]
    assert "--read-only" in calls[0]
    assert "--pull=never" in calls[0]
    assert "--cap-drop=ALL" in calls[0]


def test_invalid_converted_output_is_not_accepted(tmp_path: Path, monkeypatch) -> None:
    def run(command, **kwargs):
        mount = next(v for v in command if v.endswith("dst=/output"))
        target = mount.split("src=", 1)[1].split(",dst=", 1)[0]
        (Path(target) / "source.pdf").write_bytes(b"not pdf")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("shutil.which", lambda _: "docker")
    monkeypatch.setattr("subprocess.run", run)
    source = tmp_path / "old.doc"
    source.write_bytes(b"old")
    with pytest.raises(UnsupportedContentError, match="produce a PDF"):
        OfficeConverter("local/office@sha256:" + "a" * 64).convert(
            source, tmp_path / "out.pdf", ".doc"
        )


def test_prepare_cli_stays_offline_even_with_telemetry_configured(tmp_path: Path, monkeypatch):
    from ingest_pipeline.cli import main

    def forbidden(*args, **kwargs):
        pytest.fail("Offline intake must not configure cloud telemetry")

    monkeypatch.setattr("ingest_pipeline.cli.configure_telemetry", forbidden)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "must-not-be-used")
    source = tmp_path / "corpus"
    source.mkdir()
    (source / "a.txt").write_bytes(b"Scientific report")
    assert main([
        "prepare-corpus", "--root", str(source), "--output", str(tmp_path / "out"),
    ]) == 0
