import pytest

from ekyc.pipeline.ocr.base import OcrLine
from ekyc.pipeline.ocr.line_grouping import group_into_lines


def box(text: str, x: float, y: float, conf: float = 0.9) -> OcrLine:
    return OcrLine(text, conf, x, y, x + 10, y + 20)


def test_grouping() -> None:
    result = group_into_lines(
        [box("B", 15, 0, 0.5), box("AAA", 0, 0)], row_tolerance=0.5, gap_ratio=1.5
    )
    assert len(result) == 1 and result[0].text == "AAA B"
    assert result[0].conf == pytest.approx(0.8)
    assert result[0].x1 == 0 and result[0].x2 == 25


def test_columns_rows_order_empty() -> None:
    result = group_into_lines(
        [box("bottom", 0, 100), box("right", 100, 0), box("left", 0, 0)],
        row_tolerance=0.5,
        gap_ratio=1.5,
    )
    assert [item.text for item in result] == ["left", "right", "bottom"]
    assert group_into_lines([], row_tolerance=0.5, gap_ratio=1.5) == []


def test_raw_parts_are_preserved() -> None:
    left, right = box("A", 0, 0), box("B", 15, 0)
    assert left.parts == right.parts == ()
    result = group_into_lines([right, left], row_tolerance=0.5, gap_ratio=1.5)
    assert result[0].parts == (left, right)
    assert group_into_lines(result, row_tolerance=0.5, gap_ratio=1.5)[0].parts == (left, right)
