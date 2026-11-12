from collections.abc import Sequence
from statistics import median

from ekyc.pipeline.ocr.base import OcrLine


def group_into_lines(
    boxes: Sequence[OcrLine], *, row_tolerance: float, gap_ratio: float
) -> list[OcrLine]:
    rows: list[list[OcrLine]] = []
    for box in sorted(boxes, key=lambda item: (item.center_y, item.x1)):
        if rows:
            row = rows[-1]
            height = median(item.y2 - item.y1 for item in row)
            center = sum(item.center_y for item in row) / len(row)
            if abs(box.center_y - center) <= row_tolerance * height:
                row.append(box)
                continue
        rows.append([box])
    result = []
    for row in rows:
        row.sort(key=lambda item: item.x1)
        height = median(item.y2 - item.y1 for item in row)
        segments: list[list[OcrLine]] = []
        for box in row:
            if not segments or box.x1 - segments[-1][-1].x2 > gap_ratio * height:
                segments.append([box])
            else:
                segments[-1].append(box)
        for segment in segments:
            weight = sum(len(item.text) for item in segment)
            confidence = (
                sum(item.conf * len(item.text) for item in segment) / weight
                if weight
                else min(item.conf for item in segment)
            )
            result.append(
                OcrLine(
                    " ".join(item.text for item in segment),
                    confidence,
                    min(item.x1 for item in segment),
                    min(item.y1 for item in segment),
                    max(item.x2 for item in segment),
                    max(item.y2 for item in segment),
                    parts=tuple(
                        sorted(
                            (part for item in segment for part in _raw_parts(item)),
                            key=lambda part: part.x1,
                        )
                    ),
                )
            )
    return sorted(result, key=lambda item: (item.center_y, item.x1))


def _raw_parts(line: OcrLine) -> tuple[OcrLine, ...]:
    return (
        tuple(part for child in line.parts for part in _raw_parts(child)) if line.parts else (line,)
    )
