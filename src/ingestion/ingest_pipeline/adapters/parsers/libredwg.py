"""DWG text through GNU LibreDWG ``dwg2dxf`` (GPL-3.0-or-later) and ezdxf (MIT).

The converter is a separate executable, never linked into this process. It parses untrusted
binaries, so it runs with ``prlimit`` memory, CPU, file-size and core limits, a scrubbed
environment, a hard kill on timeout, and only scratch paths. LibreDWG reports unsupported
objects as errors even when the output is fine, so a readable DXF is the success signal.
"""

from __future__ import annotations

import asyncio
import math
import os
import signal
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...domain.errors import ConfigurationError, UnsupportedContentError
from ...ports.parsing import DrawingData, DrawingInsert, DrawingText

MIB = 1024 * 1024
ACIS = frozenset(
    {
        "3DSOLID",
        "BODY",
        "REGION",
        "SURFACE",
        "EXTRUDEDSURFACE",
        "LOFTEDSURFACE",
        "REVOLVEDSURFACE",
        "SWEPTSURFACE",
        "PLANESURFACE",
        "NURBSURFACE",
    }
)
MAX_TEXTS = 50_000
MAX_INSERTS = 10_000
LOG_TAIL_BYTES = 400


@dataclass(frozen=True)
class ConverterLimits:
    timeout_seconds: float = 60.0
    memory_bytes: int = 1536 * MIB
    """Data-segment limit. LibreDWG reserves far more address space than it uses, so an
    address-space limit fails every file; the largest pilot drawing peaked at 1.25 GB."""
    output_bytes: int = 160 * MIB
    """Largest DXF with useful text in the pilot was 86 MB; bigger ones are geometry-heavy models
    whose parse would cost the worker far more memory than their text is worth."""


def limited(command: Sequence[str], limits: ConverterLimits) -> list[str]:
    cpu = max(1, math.ceil(limits.timeout_seconds))
    return [
        "prlimit",
        f"--data={limits.memory_bytes}",
        f"--cpu={cpu}",
        f"--fsize={limits.output_bytes}",
        "--core=0",
        "--",
        *command,
    ]


def _environment() -> dict[str, str]:
    # SYSTEMROOT only matters on Windows test hosts, where Python cannot start without it.
    return {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}


async def _run(argv: Sequence[str], *, cwd: Path, log: Path, seconds: float) -> int:
    with log.open("wb") as sink:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=sink,
                stderr=sink,
            )
        except FileNotFoundError as error:
            raise ConfigurationError(f"{argv[0]} is not installed on this worker") from error
        try:
            async with asyncio.timeout(seconds):
                return await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()
            raise


def _tail(log: Path) -> str:
    with log.open("rb") as handle:
        handle.seek(max(0, log.stat().st_size - LOG_TAIL_BYTES))
        return " ".join(handle.read().decode("utf-8", errors="replace").split())


def _exit(code: int) -> str:
    if code >= 0:
        return f"exit {code}"
    try:
        return f"signal {signal.Signals(-code).name}"
    except ValueError:
        return f"signal {-code}"


class LibreDwgReader:
    """``DrawingReader`` over ``dwg2dxf``; ``resource_limits=False`` is for test hosts only."""

    def __init__(
        self,
        limits: ConverterLimits | None = None,
        *,
        command: Sequence[str] = ("dwg2dxf",),
        resource_limits: bool = True,
    ) -> None:
        self.limits = limits or ConverterLimits()
        self.command = tuple(command)
        self.resource_limits = resource_limits
        self._version: str | None = None

    async def version(self, work_dir: Path) -> str:
        if self._version is None:
            log = work_dir / "converter-version.log"
            try:
                code = await _run([*self.command, "--version"], cwd=work_dir, log=log, seconds=30)
            except TimeoutError as error:
                raise ConfigurationError("DWG converter does not answer --version") from error
            text = log.read_text(encoding="utf-8", errors="replace").strip()
            if code != 0 or not text:
                raise ConfigurationError(f"DWG converter is broken (exit {code}): {text[:200]}")
            self._version = text.splitlines()[0].strip()
        return self._version

    async def read(self, path: Path, work_dir: Path) -> DrawingData:
        converter = await self.version(work_dir)
        target = work_dir / "drawing.dxf"
        log = work_dir / "converter.log"
        argv = [*self.command, "-v0", "-y", "-o", str(target), str(path)]
        if self.resource_limits:
            argv = limited(argv, self.limits)
        try:
            code = await _run(argv, cwd=work_dir, log=log, seconds=self.limits.timeout_seconds)
        except TimeoutError as error:
            raise UnsupportedContentError(
                f"DWG conversion exceeded {self.limits.timeout_seconds:g} s"
            ) from error
        if code != 0:
            # Signals come from the limits: SIGXFSZ output size, SIGXCPU CPU, SIGSEGV memory.
            raise UnsupportedContentError(f"DWG conversion failed ({_exit(code)}): {_tail(log)}")
        if not target.is_file() or target.stat().st_size == 0:
            raise UnsupportedContentError(f"DWG conversion wrote no DXF: {_tail(log)}")
        return await asyncio.to_thread(read_dxf, target, converter)


def _point(value: Any) -> tuple[float, float]:
    return (float(value[0]), float(value[1])) if value is not None else (0.0, 0.0)


def _text(entity: Any, layout: str) -> DrawingText | None:
    from ezdxf.entities.dimension import Dimension
    from ezdxf.entities.mleader import MultiLeader
    from ezdxf.entities.mtext import MText
    from ezdxf.entities.text import Text
    from ezdxf.tools.text import plain_mtext

    value: str
    if isinstance(entity, Text):
        value, point, height = entity.plain_text(), entity.dxf.insert, entity.dxf.height
    elif isinstance(entity, MText):
        lines = entity.plain_text(split=False)
        value = lines if isinstance(lines, str) else " ".join(lines)
        point, height = entity.dxf.insert, entity.dxf.char_height
    elif isinstance(entity, Dimension):
        # Only overridden dimension text is text; "<>" stands for the measured value.
        raw = entity.dxf.get("text", "") or ""
        if raw.strip() in ("", "<>"):
            return None
        value = str(plain_mtext(raw.replace("<>", "")))
        point = entity.dxf.get("text_midpoint") or entity.dxf.get("defpoint")
        height = 0.0
    elif isinstance(entity, MultiLeader):
        mtext = entity.context.mtext
        if mtext is None:
            return None
        value = str(plain_mtext(mtext.default_content))
        point, height = mtext.insert, entity.context.char_height
    else:
        return None
    value = " ".join(value.split())
    if not value:
        return None
    x, y = _point(point)
    return DrawingText(
        layout=layout, entity=entity.dxftype(), text=value, x=x, y=y, height=float(height or 0)
    )


def read_dxf(path: Path, converter: str) -> DrawingData:
    """Text, attributes and inventory of a DXF; geometry is counted, never interpreted."""

    from ezdxf import recover
    from ezdxf.entities.insert import Insert

    try:
        doc, _ = recover.readfile(str(path))
    except Exception as error:  # any parser failure on converted bytes means unreadable
        raise UnsupportedContentError(
            f"DXF output is unreadable: {type(error).__name__}: {str(error)[:200]}"
        ) from error
    texts: list[DrawingText] = []
    inserts: list[DrawingInsert] = []
    layouts: list[str] = []
    xrefs: set[str] = set()
    warnings: list[str] = []
    entities = solids = skipped = 0
    for layout in doc.layouts:
        name = "Model" if layout.is_modelspace else layout.name
        count = 0
        for entity in layout:
            count += 1
            kind = entity.dxftype()
            if kind in ACIS:
                solids += 1
                continue
            try:
                if isinstance(entity, Insert):
                    block = doc.blocks.get(entity.dxf.name)
                    if block is not None and block.block is not None and block.block.dxf.flags & 4:
                        xrefs.add(entity.dxf.name)
                    values = tuple(
                        (attrib.dxf.tag.upper(), " ".join(attrib.plain_text().split()))
                        for attrib in entity.attribs
                    )
                    values = tuple((tag, value) for tag, value in values if value)
                    if values and len(inserts) < MAX_INSERTS:
                        inserts.append(DrawingInsert(name, entity.dxf.name, values))
                    elif values and "attributes truncated" not in warnings:
                        warnings.append("attributes truncated")
                    continue
                text = _text(entity, name)
            except Exception:  # one malformed entity must not lose the drawing
                skipped += 1
                continue
            if text is not None:
                if len(texts) < MAX_TEXTS:
                    texts.append(text)
                elif "text truncated" not in warnings:
                    warnings.append("text truncated")
        entities += count
        if count:
            layouts.append(name)
    if skipped:
        warnings.append(f"{skipped} unreadable entities skipped")
    codepage = doc.header.get("$DWGCODEPAGE")
    return DrawingData(
        converter=converter,
        dxf_version=doc.dxfversion,
        codepage=str(codepage) if codepage else None,
        layouts=tuple(layouts),
        entities=entities,
        texts=tuple(texts),
        inserts=tuple(inserts),
        solids=solids,
        xrefs=tuple(sorted(xrefs)),
        warnings=tuple(warnings),
    )
