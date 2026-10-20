"""Container entry point. The caller supplies network, memory, process and time limits.

Single mode: ``convert.py /input/source.doc`` writes ``/output/source.pdf``.
Batch mode: ``convert.py --batch IN OUT`` converts every ``<sha256>.doc|.ppt`` in IN to
``OUT/<sha256>.pdf`` with a fresh LibreOffice profile and timeout per file, and records each
outcome in ``OUT/results.json``. Failures are recorded, never silently skipped.
"""

import json
import os
import pathlib
import re
import resource
import shutil
import signal
import subprocess
import sys

MAX_BYTES = 1024**3
TIMEOUT_SECONDS = 110
SOFFICE = "/usr/bin/soffice"
NAME = re.compile(r"^[0-9a-f]{64}\.(doc|ppt)$")
PROFILE = (
    '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
    '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
    '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
    '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
    "</item>"
    '<item oor:path="/org.openoffice.Office.Calc/Content/Update">'
    '<prop oor:name="Link" oor:op="fuse"><value>2</value></prop>'
    "</item></oor:items>"
)


def convert(source: pathlib.Path, outdir: pathlib.Path, profile: pathlib.Path) -> int:
    user = profile / "user"
    user.mkdir(parents=True)
    user.joinpath("registrymodifications.xcu").write_text(PROFILE, encoding="utf-8")
    process = subprocess.Popen(  # noqa: S603 - fixed executable and mounted input, no shell
        [SOFFICE, f"-env:UserInstallation=file://{profile}", "--headless", "--nologo",
         "--nodefault", "--norestore", "--convert-to", "pdf", "--outdir", str(outdir),
         str(source)],
        start_new_session=True,
    )
    try:
        return process.wait(timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        raise


def batch(inputs: pathlib.Path, outputs: pathlib.Path) -> int:
    results: dict[str, dict[str, str]] = {}
    for source in sorted(inputs.iterdir()):
        if not NAME.match(source.name):
            continue
        digest = source.name.split(".", 1)[0]
        work = pathlib.Path("/tmp") / f"work-{digest}"  # noqa: S108 - private container tmpfs
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir()
        try:
            code = convert(source, work / "out", work / "profile")
            produced = work / "out" / f"{digest}.pdf"
            if code or not produced.is_file():
                raise RuntimeError(f"LibreOffice exit {code}; no PDF produced")
            if produced.stat().st_size > MAX_BYTES:
                raise RuntimeError("Converted PDF exceeds the size limit")
            with produced.open("rb") as handle:
                if not handle.read(8).startswith(b"%PDF-"):
                    raise RuntimeError("Output is not a PDF")
            shutil.move(str(produced), outputs / f"{digest}.pdf")
            results[digest] = {"status": "converted"}
        except (subprocess.TimeoutExpired, RuntimeError, OSError) as error:
            results[digest] = {"status": "failed", "reason": str(error)[:500]}
        finally:
            shutil.rmtree(work, ignore_errors=True)
    outputs.joinpath("results.json").write_text(
        json.dumps(results, indent=2, sort_keys=True), encoding="utf-8"
    )
    failed = sum(item["status"] == "failed" for item in results.values())
    print(json.dumps({"converted": len(results) - failed, "failed": failed}))
    return 0


def main() -> int:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES, MAX_BYTES))
    if sys.argv[1:2] == ["--batch"]:
        return batch(pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3]))
    profile = pathlib.Path("/tmp/profile")  # noqa: S108 - private container tmpfs
    return convert(pathlib.Path(sys.argv[1]), pathlib.Path("/output"), profile)


if __name__ == "__main__":
    raise SystemExit(main())
