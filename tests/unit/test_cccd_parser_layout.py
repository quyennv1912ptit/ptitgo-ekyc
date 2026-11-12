from datetime import date

import pytest

from ekyc.config import Settings
from ekyc.pipeline.ocr.base import OcrLine
from ekyc.pipeline.parsers.cccd_parser import ParseDebug, parse_cccd_front

TODAY = date(2026, 11, 10)


def line(text: str, y: float, conf: float = 0.9, x1: float = 100, x2: float = 400) -> OcrLine:
    return OcrLine(text, conf, x1, y, x2, y + 20)


@pytest.mark.parametrize("inline", [False, True])
def test_origin_scrub(settings: Settings, inline: bool) -> None:
    lines = (
        [line("Quê quán / Place of ongin: Thanh Hóa", 100)]
        if inline
        else [line("Quê quán / Place of ongin:", 100), line("Thanh Hóa", 130)]
    )
    debug = ParseDebug()
    result = parse_cccd_front(lines, settings, TODAY, debug)
    assert result.origin.value == "Thanh Hóa"
    assert result.origin.raw == "Thanh Hóa"
    assert result.origin.status == "parsed"
    assert any(key == "origin" and "ongin" in text for key, text in debug.scrubbed)


def test_two_columns(settings: Settings) -> None:
    lines = [
        line("Họ và tên: NGUYEN VAN A", 20, x1=380),
        line("Ngày sinh: 01/01/2000", 50, x1=380),
        line("Nơi thường trú / Place ot residence:", 100, x1=380),
        line("Thôn Phúc Lộc", 130, 0.95, x1=380),
        line("Có giá trị đến", 150, 0.2, x1=40, x2=250),
        line("Dale of expiry:", 160, 0.1, x1=40, x2=250),
        line("Xã Quảng Phú", 165, 0.92, x1=380),
        line("12/05/2035", 190, 0.9, x1=40, x2=250),
    ]
    debug = ParseDebug()
    result = parse_cccd_front(lines, settings, TODAY, debug)
    assert result.address.value == "Thôn Phúc Lộc, Xã Quảng Phú"
    assert result.address.conf == 0.92
    assert result.expiry.value == date(2035, 5, 12)
    assert result.layout_reliable
    assert all(debug.fragments[i].column == "right" for i in debug.used_fragments["address"])


def test_stop_at_other_sublabel(settings: Settings) -> None:
    result = parse_cccd_front(
        [
            line("Nơi thường trú", 100),
            line("Thôn Phúc Lộc", 130),
            line("Dale of expiry", 160),
            line("12/05/2035", 190),
        ],
        settings,
        TODAY,
    )
    assert result.address.value == "Thôn Phúc Lộc"
    assert result.expiry.value == date(2035, 5, 12)


def test_english_fallback(settings: Settings) -> None:
    result = parse_cccd_front(
        [line("No1 thu0ng tr", 100), line("Place of residence:", 130), line("Thôn Phúc Lộc", 160)],
        settings,
        TODAY,
    )
    assert result.address.value == "Thôn Phúc Lộc"


def test_label_only_does_not_lower_conf(settings: Settings) -> None:
    lines = [line("Quê quán", 100), line("Place of ongin:", 130, 0.1), line("Thanh Hóa", 160, 0.96)]
    result = parse_cccd_front(lines, settings, TODAY)
    assert result.origin.value == "Thanh Hóa" and result.origin.conf == 0.96


def test_empty_after_scrub(settings: Settings) -> None:
    result = parse_cccd_front([line("Quê quán: Place of ongin:", 100)], settings, TODAY)
    assert result.origin.raw is None and result.origin.status == "missing"


def test_name_no_vietnamese_scrub(settings: Settings) -> None:
    result = parse_cccd_front([line("Quê quán: Hòa Vang, Đà Nẵng", 100)], settings, TODAY)
    assert result.origin.value == "Hòa Vang, Đà Nẵng"


def test_short_own_sublabel(settings: Settings) -> None:
    result = parse_cccd_front(
        [line("Giới tính", 100), line("Sex:", 130, 0.1), line("Nữ", 160, 0.95)], settings, TODAY
    )
    assert result.gender.value == "FEMALE"
    assert result.gender.conf == 0.95


def test_english_only_label(settings: Settings) -> None:
    result = parse_cccd_front(
        [line("Place of residence:", 100), line("Thanh Hóa", 130)], settings, TODAY
    )
    assert result.address.value == "Thanh Hóa"
