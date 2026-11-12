from datetime import date

import pytest

from ekyc.pipeline.ocr.base import OcrLine
from ekyc.pipeline.parsers.cccd_checks import check_id_structure
from ekyc.pipeline.parsers.cccd_normalizers import (
    clean_address,
    clean_name,
    extract_id_number,
    fix_digit_confusions,
    is_no_expiry,
    parse_date,
    parse_gender,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("01/05/1999", date(1999, 5, 1)),
        ("1-5-1999", date(1999, 5, 1)),
        ("0l/O5/l999", date(1999, 5, 1)),
        ("31/02/2000", None),
        ("01 / 05 . 1999", date(1999, 5, 1)),
    ],
)
def test_dates(text: str, expected: date | None) -> None:
    assert parse_date(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("079200123456", "079200123456"),
        ("079 200 123 456", "079200123456"),
        ("07912345678O", "079123456780"),
        ("0792001234567", None),
        ("OOOOOOOOOOOO", None),
    ],
)
def test_ids(text: str, expected: str | None) -> None:
    result = extract_id_number([OcrLine(text, 0.9, 0, 0, 200, 20)])
    assert (result[0] if result else None) == expected


def test_best_id() -> None:
    lines = [
        OcrLine(text, conf, 0, y, 200, y + 20)
        for text, conf, y in [
            ("079200123456", 0.8, 100),
            ("079200123457", 0.9, 60),
            ("079200123458", 0.9, 20),
        ]
    ]
    assert extract_id_number(lines) == ("079200123458", 0.9)


@pytest.mark.parametrize(
    "text,expected",
    [("Nam", "MALE"), ("Nữ", "FEMALE"), ("Nam Quốc tịch Việt Nam", "MALE"), ("Việt Nam", None)],
)
def test_gender(text: str, expected: str | None) -> None:
    assert parse_gender(text) == expected


def test_cleaners() -> None:
    assert clean_name("  nguyễn văn a! ") == "NGUYỄN VĂN A"
    assert clean_name("NGUYEN VAN A1") == ""
    assert clean_address([" , Hà Nội, ", " Quận  1 "]) == "Hà Nội, Quận 1"
    assert is_no_expiry("Không thời hạn")
    assert fix_digit_confusions("OolI|SBZ") == "00111582"


@pytest.mark.parametrize(
    "number,dob,gender,expected",
    [
        ("079200123456", date(2000, 1, 1), "MALE", []),
        ("001185123456", date(1985, 1, 1), "FEMALE", []),
        ("079200123456", date(2001, 1, 1), "MALE", ["birth_year"]),
        ("079200123456", date(2000, 1, 1), "FEMALE", ["gender"]),
        ("079400123456", date(1980, 1, 1), "FEMALE", []),
        ("079200123456", date(2000, 1, 1), None, []),
    ],
)
def test_structure(number: str, dob: date, gender: str | None, expected: list[str]) -> None:
    assert check_id_structure(number, dob, gender) == expected
