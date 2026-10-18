"""Container entry point. The caller supplies network, memory, process and time limits."""

import pathlib
import resource
import subprocess
import sys

resource.setrlimit(resource.RLIMIT_FSIZE, (1024**3, 1024**3))
profile = pathlib.Path("/tmp/profile/user")  # noqa: S108 - private container tmpfs
profile.mkdir(parents=True)
profile.joinpath("registrymodifications.xcu").write_text(
    '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
    '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
    '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
    '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
    '</item>'
    '<item oor:path="/org.openoffice.Office.Calc/Content/Update">'
    '<prop oor:name="Link" oor:op="fuse"><value>2</value></prop>'
    '</item></oor:items>',
    encoding="utf-8",
)
result = subprocess.run(  # noqa: S603 - fixed executable and mounted input, no shell
    ["/usr/bin/soffice", "-env:UserInstallation=file:///tmp/profile", "--headless", "--nologo",
     "--nodefault", "--norestore", "--convert-to", "pdf", "--outdir", "/output", sys.argv[1]],
    check=False,
    timeout=110,
)
raise SystemExit(result.returncode)
