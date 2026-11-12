from datetime import date

import pytest

from ekyc.config import Settings
from ekyc.pipeline.ocr.base import OcrLine
from ekyc.pipeline.parsers.cccd_parser import ParseDebug, compute_columns, parse_cccd_front

TODAY = date(2026, 11, 10)


def line(
    text: str,
    y: float,
    x1: float = 380,
    x2: float = 720,
    conf: float = 0.95,
    parts: tuple[OcrLine, ...] = (),
) -> OcrLine:
    return OcrLine(text, conf, x1, y, x2, y + 25, parts)


def exclusive(debug: ParseDebug) -> None:
    used = [index for indices in debug.used_fragments.values() for index in indices]
    assert len(used) == len(set(used))
    assert all(0 <= index < len(debug.fragments) for index in used)


def anchors() -> list[OcrLine]:
    return [
        line("Họ và tên: NGUYEN VAN A", 140),
        line("Ngày sinh: 01/01/2000", 190),
        line("Giới tính: Nam", 240),
    ]


def test_compute_columns(settings: Settings) -> None:
    lines = [
        line("Họ và tên", 140, 380),
        line("Ngày sinh", 190, 400),
        line("Giới tính", 240, 360),
        line("12/05/2035 Nơi thường trú", 365, 40),
    ]
    assert compute_columns(lines, settings) == (380, 317.5)
    assert compute_columns(lines[-1:], settings) == (None, None)


@pytest.mark.parametrize(
    "raw_merged,inline,interleaved",
    [(False, False, False), (True, False, False), (True, True, False), (False, False, True)],
)
def test_two_column_flow(
    settings: Settings, raw_merged: bool, inline: bool, interleaved: bool
) -> None:
    # Có phần địa chỉ trùng quê quán hợp lệ; tuyệt đối không loại trùng theo nội dung.
    first = "Thôn Phúc Lộc, Xã Quảng Phú,"
    second = "Quảng Phú, Quảng Xương, Thanh Hóa"
    left = line("12/05/2035", 365, 40, 160)
    right = line("Nơi thường trú / Place of residence:" + (" " + first if inline else ""), 365)
    merged = line(
        left.text + " " + right.text, 365, 40, 720, parts=() if raw_merged else (left, right)
    )
    lines = anchors() + [
        line("Quê quán / Place of ongin:", 300),
        line(second, 330),
        line("Có giá trị đến", 335, 40, 200),
        line("Date of expiry", 350, 40, 200),
        merged,
    ]
    if not inline:
        lines.append(line(first, 395))
    if interleaved:
        lines.append(line("Thông tin cột trái", 410, 40, 250, 0.1))
    lines.append(line(second, 425))
    debug = ParseDebug()
    result = parse_cccd_front(lines, settings, TODAY, debug)
    assert result.layout_reliable
    assert result.origin.value == second
    assert result.address.value == first.rstrip(",") + ", " + second
    assert result.expiry.value == date(2035, 5, 12)
    assert "Date" not in result.address.raw and "expiry" not in result.origin.raw
    assert result.address.conf == 0.95
    assert debug.label_splits == ([7] if raw_merged else [])
    exclusive(debug)
    assert all(debug.fragments[index].column == "right" for index in debug.used_fragments["origin"])
    assert all(debug.fragments[index].column == "left" for index in debug.used_fragments["expiry"])


def test_insufficient_anchors_and_single_column(settings: Settings) -> None:
    for lines in (
        [line("Quê quán: Thanh Hóa", 300), line("12/05/2035 Nơi thường trú: Hà Nội", 365, 40)],
        [
            line("Họ và tên: NGUYEN VAN A", 140),
            line("Ngày sinh: 01/01/2000", 190),
            line("Quê quán: Thanh Hóa", 300),
            line("Nơi thường trú: Hà Nội", 365),
            line("Có giá trị đến: 12/05/2035", 425),
        ],
    ):
        debug = ParseDebug()
        result = parse_cccd_front(lines, settings, TODAY, debug)
        assert not result.layout_reliable
        assert result.origin.value == "Thanh Hóa" and result.address.value == "Hà Nội"
        exclusive(debug)


def test_english_label_value_after_primary(settings: Settings) -> None:
    lines = anchors() + [
        line("Quê quán", 300),
        line("Place of ongin: Thanh Hóa", 330),
        line("Có giá trị đến", 335, 40, 200),
        line("Date of expiry: 12/05/2035", 365, 40, 250),
        line("Nơi thường trú: Hà Nội", 395),
    ]
    debug = ParseDebug()
    result = parse_cccd_front(lines, settings, TODAY, debug)
    assert result.origin.value == "Thanh Hóa"
    assert result.expiry.value == date(2035, 5, 12)
    assert result.address.value == "Hà Nội"
    exclusive(debug)
