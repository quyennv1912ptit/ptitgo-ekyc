from datetime import date

import pytest

from ekyc.config import Settings
from ekyc.pipeline.ocr.base import OcrLine
from ekyc.pipeline.parsers.cccd_parser import parse_cccd_back, parse_cccd_front

TODAY = date(2026, 11, 10)


def make_lines(texts: list[str]) -> list[OcrLine]:
    return [
        OcrLine(text, 0.9, 0, index * 30, 600, index * 30 + 20) for index, text in enumerate(texts)
    ]


def test_inline(settings: Settings) -> None:
    lines = make_lines(
        [
            "079200123456",
            "Họ và tên: NGUYỄN VĂN A",
            "Ngày sinh: 01/05/2000",
            "Giới tính / Sex: Nữ Quốc tịch / Nationality: Việt Nam",
            "Quê quán: Hà Nội",
            "Nơi thường trú: Quận 1",
            "Thành phố A",
            "Có giá trị đến: 01/05/2035",
        ]
    )
    result = parse_cccd_front(lines, settings, TODAY)
    assert result.id_number.value == "079200123456"
    assert result.full_name.value == "NGUYỄN VĂN A"
    assert result.dob.value == date(2000, 5, 1)
    assert result.gender.value == "FEMALE"
    assert result.origin.value == "Hà Nội"
    assert result.address.value == "Quận 1, Thành phố A"
    assert result.expiry.value == date(2035, 5, 1)
    assert result == parse_cccd_front(lines, settings, TODAY)


@pytest.mark.parametrize(
    "texts",
    [
        ["Họ và tên", "NGUYEN VAN A"],
        ["Họ và tên", "Full name:", "NGUYEN VAN A"],
        ["Họ và tên / Full name: NGUYEN VAN A"],
        ["Ho va ten: NGUYEN VAN A"],
    ],
)
def test_name_layouts(settings: Settings, texts: list[str]) -> None:
    assert parse_cccd_front(make_lines(texts), settings, TODAY).full_name.value == "NGUYEN VAN A"


def test_missing_invalid_no_expiry(settings: Settings) -> None:
    result = parse_cccd_front(
        make_lines(["Ngày sinh: 01/01/2099", "Có giá trị đến: Không thời hạn"]), settings, TODAY
    )
    assert result.dob.status == "invalid"
    assert result.full_name.status == "missing"
    assert result.no_expiry and result.expiry.status == "parsed" and result.expiry.value is None
    result = parse_cccd_front(make_lines(["Quốc tịch: Việt Nam"]), settings, TODAY)
    assert result.gender.status == "missing"


def test_multiline_conf_and_gap(settings: Settings) -> None:
    lines = make_lines(["Quê quan: Hà Nội", "Huyện A", "Có giá trị đến: 01/01/2035"])
    lines[1] = OcrLine(lines[1].text, 0.4, 0, 30, 600, 50)
    result = parse_cccd_front(lines, settings, TODAY)
    assert result.origin.value == "Hà Nội, Huyện A"
    assert result.origin.conf == 0.4
    lines[1] = OcrLine("Huyện A", 0.4, 0, 300, 600, 320)
    assert parse_cccd_front(lines, settings, TODAY).origin.value == "Hà Nội"


def test_invalid_dates_and_name(settings: Settings) -> None:
    result = parse_cccd_front(
        make_lines(["Họ và tên: A123", "Ngày sinh: 31/02/2000", "Có giá trị đến: abc"]),
        settings,
        TODAY,
    )
    assert result.full_name.status == result.dob.status == result.expiry.status == "invalid"
    result = parse_cccd_front(
        make_lines(["Ngày sinh: 01/01/2000", "Có giá trị đến: 01/01/1999"]), settings, TODAY
    )
    assert result.expiry.status == "invalid"


def test_back(settings: Settings) -> None:
    assert parse_cccd_back(make_lines(["Ngày 01/01/2020"]), settings).issue_date.value == date(
        2020, 1, 1
    )
    assert parse_cccd_back(make_lines(["Không có ngày"]), settings).issue_date.status == "missing"
