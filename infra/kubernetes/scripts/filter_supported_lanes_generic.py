"""Filter a Blob manifest JSONL to only lanes with an implemented extractor
(document_layout, drawing_pipeline), and print an exception summary for the rest
(archive_expansion, spreadsheet, native_conversion, image_ocr, excluded).

Generic version: takes input/output paths as argv[1]/argv[2].
"""

from __future__ import annotations

import json
import sys
from collections import Counter

from ingestion.models import ProcessingLane, SourceObject
from ingestion.path_metadata import parse_placement
from ingestion.routing import route_placement

_SUPPORTED_LANES = {ProcessingLane.DOCUMENT_LAYOUT, ProcessingLane.DRAWING_PIPELINE}

input_path = sys.argv[1]
output_path = sys.argv[2]

supported_count = 0
excluded_by_lane: Counter[str] = Counter()

with open(input_path, encoding="utf-8") as in_stream, open(
    output_path, "w", encoding="utf-8"
) as out_stream:
    for line in in_stream:
        line = line.strip()
        if not line:
            continue
        source = SourceObject.model_validate_json(line)
        placement = parse_placement(source)
        decision = route_placement(source, placement)
        if decision.lane in _SUPPORTED_LANES:
            supported_count += 1
            out_stream.write(line + "\n")
        else:
            excluded_by_lane[decision.lane.value] += 1

print(
    json.dumps(
        {
            "supported_count": supported_count,
            "excluded_by_lane": dict(excluded_by_lane),
        },
        ensure_ascii=False,
    )
)
