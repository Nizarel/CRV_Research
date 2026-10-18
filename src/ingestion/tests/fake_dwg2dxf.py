"""Stand-in for LibreDWG ``dwg2dxf`` in tests: ``fake_dwg2dxf.py MODE [dwg2dxf arguments]``.

Modes: ``copy:<dxf>`` writes that DXF to ``-o``; ``sleep`` hangs; ``fail`` exits 1;
``garbage`` writes an unreadable DXF; ``broken`` also fails ``--version``.
"""

import shutil
import sys
import time

mode, arguments = sys.argv[1], sys.argv[2:]
if arguments == ["--version"]:
    if mode == "broken":
        sys.exit(2)
    print("dwg2dxf 0.14 (fake)")
    sys.exit(0)
target = arguments[arguments.index("-o") + 1]
if mode.startswith("copy:"):
    shutil.copyfile(mode.removeprefix("copy:"), target)
elif mode == "sleep":
    time.sleep(60)
elif mode == "garbage":
    with open(target, "w", encoding="ascii") as handle:
        handle.write("not a dxf at all\n")
else:
    print("ERROR: boom, DWG header is damaged", file=sys.stderr)
    sys.exit(1)
