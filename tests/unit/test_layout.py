import unicodedata

import pytest

from ekyc.pipeline.layout import build_fragments
from ekyc.pipeline.ocr.base import OcrLine


def box(
    text: str,
    x1: float,
    x2: float,
    y: float = 100,
    conf: float = 0.9,
    parts: tuple[OcrLine, ...] = (),
) -> OcrLine:
    return OcrLine(text, conf, x1, y, x2, y + 25, parts)


def test_no_split() -> None:
    item = box("abc", 40, 720, parts=(box("a", 40, 100), box("bc", 380, 720)))
    result = build_fragments([item], x_split=None)
    assert len(result) == 1 and result[0].column == "right"
    assert result[0].text == "abc" and result[0].conf == item.conf


def test_parts_and_order() -> None:
    left = box("AAA", 40, 160, conf=0.8)
    right1, right2 = box("B", 380, 400, conf=0.5), box("CCC", 410, 480, conf=0.9)
    merged = box("AAA B CCC", 40, 480, parts=(right2, left, right1))
    fragments = build_fragments([box("late", 380, 700, 200), merged], x_split=300)
    assert [item.column for item in fragments] == ["left", "right", "right"]
    assert fragments[1].text == "B CCC" and fragments[1].conf == pytest.approx(0.8)
    assert [item.source for item in fragments] == [1, 1, 0]
    assert not any(item.estimated for item in fragments)


def test_label_fallback() -> None:
    text = unicodedata.normalize("NFD", "12/05/2035 Nơi thường trú")

    def offset(value: str) -> int:
        return value.find("Nơi")

    fragments = build_fragments([box(text, 40, 720)], x_split=300, x_right=380, split_offset=offset)
    assert len(fragments) == 2 and all(item.estimated for item in fragments)
    assert fragments[0].text == "12/05/2035"
    assert fragments[1].text == "Nơi thường trú" and fragments[1].x1 == 380
    assert fragments[0].x2 == pytest.approx(40 + 11 / len(unicodedata.normalize("NFC", text)) * 680)
    assert fragments[0].conf == fragments[1].conf == 0.9


@pytest.mark.parametrize(
    "x,text,offset", [(40, ": label", 2), (380, "12 label", 3), (40, "label", 0), (40, "label", 5)]
)
def test_no_fallback_split(x: float, text: str, offset: int) -> None:
    result = build_fragments(
        [box(text, x, 720)], x_split=300, x_right=380, split_offset=lambda _: offset
    )
    assert len(result) == 1 and not result[0].estimated
