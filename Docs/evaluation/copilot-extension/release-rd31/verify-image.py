import hashlib, importlib, json, os, pkgutil, sys
from pathlib import Path

expected = json.loads(sys.argv[1])
site = Path("/usr/local/lib/python3.12/site-packages/crv_agent")
app = Path("/app/crv_agent")
mismatch = [
    f"{root.name}:{name}" for root in (site, app) for name, digest in expected.items()
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest
]
imported = []
for module in pkgutil.walk_packages([str(site)], "crv_agent."):
    importlib.import_module(module.name)
    imported.append(module.name)
from crv_agent.domain.answers import Verdict
from crv_agent.domain.evidence import Calculation
digests = {
    str(p.relative_to(site)): hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    for p in site.rglob("*.py")
}
print(json.dumps({
    "uid": os.getuid(), "cwd": os.getcwd(), "mismatch": mismatch, "imported": len(imported),
    "binding": "unsupported_bindings" in Verdict.model_fields,
    "approximate": "approximate" in Calculation.model_fields, "package": digests,
}))
