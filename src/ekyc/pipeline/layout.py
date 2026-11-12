import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from ekyc.pipeline.ocr.base import OcrLine


@dataclass(frozen=True, slots=True)
class Fragment:
    text: str
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float
    column: Literal["left", "right"]
    source: int
    estimated: bool = False

    @property
    def center_y(self) -> float:
        return (self.y1 + self.y2) / 2


def _fragment(parts: Sequence[OcrLine], column: Literal["left", "right"], source: int) -> Fragment:
    weight = sum(len(part.text) for part in parts)
    conf = (
        parts[0].conf
        if len(parts) == 1
        else (
            sum(part.conf * len(part.text) for part in parts) / weight
            if weight
            else min(part.conf for part in parts)
        )
    )
    return Fragment(
        unicodedata.normalize("NFC", " ".join(part.text for part in parts)),
        conf,
        min(part.x1 for part in parts),
        min(part.y1 for part in parts),
        max(part.x2 for part in parts),
        max(part.y2 for part in parts),
        column,
        source,
    )


def build_fragments(
    lines: Sequence[OcrLine],
    *,
    x_split: float | None,
    x_right: float | None = None,
    split_offset: Callable[[str], int | None] | None = None,
) -> list[Fragment]:
    result: list[Fragment] = []
    for source, line in enumerate(lines):
        groups: list[list[OcrLine]] = []
        if x_split is None:
            fragments = [_fragment((line,), "right", source)]
        else:
            parts = sorted(line.parts, key=lambda part: part.x1) if len(line.parts) >= 2 else [line]
            for part in parts:
                if not groups or (groups[-1][0].x1 >= x_split) != (part.x1 >= x_split):
                    groups.append([part])
                else:
                    groups[-1].append(part)
            fragments = [
                _fragment(group, "right" if group[0].x1 >= x_split else "left", source)
                for group in groups
            ]
        for fragment in fragments:
            k = (
                split_offset(fragment.text)
                if fragment.column == "left" and split_offset is not None and x_right is not None
                else None
            )
            if (
                k is not None
                and 0 < k < len(fragment.text)
                and sum(char.isalnum() for char in fragment.text[:k]) >= 2
            ):
                edge = fragment.x1 + k / len(fragment.text) * (fragment.x2 - fragment.x1)
                result.extend(
                    (
                        Fragment(
                            fragment.text[:k].strip(),
                            fragment.conf,
                            fragment.x1,
                            fragment.y1,
                            edge,
                            fragment.y2,
                            "left",
                            source,
                            True,
                        ),
                        Fragment(
                            fragment.text[k:].strip(),
                            fragment.conf,
                            x_right,
                            fragment.y1,
                            fragment.x2,
                            fragment.y2,
                            "right",
                            source,
                            True,
                        ),
                    )
                )
            else:
                result.append(fragment)
    return sorted(result, key=lambda item: (item.center_y, item.x1))
