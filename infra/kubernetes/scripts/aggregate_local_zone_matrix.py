"""Aggregate a local corpus manifest JSONL (from `manifest-local`) into a per-zone matrix.

Zone = the first path segment of `blob_name` (which is the relative path from the
corpus root when this manifest was produced via `manifest-local`).
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

from ingestion.models import SourceObject
from ingestion.path_metadata import parse_placement
from ingestion.routing import route_placement


def main(manifest_path: str, output_path: str) -> None:
    zones: dict[str, dict] = defaultdict(
        lambda: {
            "total_files": 0,
            "total_size_bytes": 0,
            "by_extension": Counter(),
            "by_route": Counter(),
            "by_lane": Counter(),
            "by_quarantine_reason": Counter(),
            "non_ascii_paths": 0,
        }
    )

    total_files = 0
    total_size = 0

    with open(manifest_path, encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            source = SourceObject.model_validate_json(line)
            zone = source.blob_name.split("/", 1)[0] if source.blob_name else "(root)"
            entry = zones[zone]
            entry["total_files"] += 1
            entry["total_size_bytes"] += source.size_bytes
            entry["by_extension"][source.extension or "(none)"] += 1
            entry["by_route"][source.route.value] += 1
            if source.quarantine_reason:
                entry["by_quarantine_reason"][source.quarantine_reason] += 1
            placement = parse_placement(source)
            decision = route_placement(source, placement)
            entry["by_lane"][decision.lane.value] += 1
            try:
                source.blob_name.encode("ascii")
            except UnicodeEncodeError:
                entry["non_ascii_paths"] += 1

            total_files += 1
            total_size += source.size_bytes

    result = {
        "total_files": total_files,
        "total_size_bytes": total_size,
        "zone_count": len(zones),
        "zones": {
            zone: {
                "total_files": entry["total_files"],
                "total_size_bytes": entry["total_size_bytes"],
                "total_size_gib": round(entry["total_size_bytes"] / (1024**3), 3),
                "by_extension": dict(entry["by_extension"].most_common()),
                "by_route": dict(entry["by_route"].most_common()),
                "by_lane": dict(entry["by_lane"].most_common()),
                "by_quarantine_reason": dict(entry["by_quarantine_reason"].most_common()),
                "non_ascii_paths": entry["non_ascii_paths"],
            }
            for zone, entry in sorted(zones.items())
        },
    }

    Path(output_path).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"total_files": total_files, "zone_count": len(zones), "output": output_path}))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
