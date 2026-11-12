from datetime import date

import pytest

from ekyc.config import Settings
from ekyc.pipeline.ocr.base import OcrLine
from ekyc.pipeline.parsers.gplx_parser import parse_gplx_front

TODAY = date(2026, 11, 12)


def line(text: str, x1: float = 10, y1: float = 10, x2: float = 400, y2: float = 40) -> OcrLine:
    return OcrLine(text, 0.9, x1, y1, x2, y2)


def test_label_and_value_same_line(settings: Settings) -> None:
    parsed = parse_gplx_front([line("Số/No: 123456789012")], settings, TODAY)
    assert parsed.id_number.status == "parsed"
    assert parsed.id_number.value == "123456789012"


def test_value_on_next_line(settings: Settings) -> None:
    lines = [line("Số/No", y1=10, y2=40), line("123456789012", x1=12, y1=50, y2=80)]
    parsed = parse_gplx_front(lines, settings, TODAY)
    assert parsed.id_number.status == "parsed"
    assert parsed.id_number.value == "123456789012"


def test_value_on_same_row_to_the_right(settings: Settings) -> None:
    lines = [line("Họ tên", x1=10, x2=120, y1=10, y2=40), line("NGUYEN VAN A", x1=200, y1=9, y2=39)]
    parsed = parse_gplx_front(lines, settings, TODAY)
    assert parsed.full_name.status == "parsed"
    assert parsed.full_name.value == "NGUYEN VAN A"


def test_bilingual_label_split_across_lines(settings: Settings) -> None:
    lines = [line("Họ tên", y1=10, y2=40), line("Full name: NGUYEN VAN A", x1=12, y1=50, y2=80)]
    parsed = parse_gplx_front(lines, settings, TODAY)
    assert parsed.full_name.status == "parsed"
    assert parsed.full_name.value == "NGUYEN VAN A"


def test_bilingual_label_on_same_line(settings: Settings) -> None:
    parsed = parse_gplx_front([line("Họ tên Full name: NGUYEN VAN A")], settings, TODAY)
    assert parsed.full_name.value == "NGUYEN VAN A"


def test_vehicle_class_with_ocr_noise(settings: Settings) -> None:
    parsed = parse_gplx_front([line("Hạng Class: Al")], settings, TODAY)
    assert parsed.vehicle_class.status == "parsed"
    assert parsed.vehicle_class.value == "A1"


def test_vehicle_class_unknown_code_is_invalid(settings: Settings) -> None:
    parsed = parse_gplx_front([line("Hạng Class: X9")], settings, TODAY)
    assert parsed.vehicle_class.status == "invalid"


def test_expiry_as_date(settings: Settings) -> None:
    parsed = parse_gplx_front([line("Có giá trị đến: 01/01/2030")], settings, TODAY)
    assert parsed.no_expiry is False
    assert parsed.expiry.value == date(2030, 1, 1)


@pytest.mark.parametrize("text", ["Không giới hạn", "Không thời hạn"])
def test_expiry_no_limit(settings: Settings, text: str) -> None:
    parsed = parse_gplx_front([line(f"Ngày hết hạn: {text}")], settings, TODAY)
    assert parsed.no_expiry is True
    assert parsed.expiry.status == "parsed"


def test_license_number_digit_confusions(settings: Settings) -> None:
    parsed = parse_gplx_front([line("Số/No: O12345I789O1")], settings, TODAY)
    assert parsed.id_number.status == "parsed"
    assert parsed.id_number.value == "012345178901"


def test_name_with_digits_is_invalid(settings: Settings) -> None:
    parsed = parse_gplx_front([line("Họ tên: NGUYEN 123")], settings, TODAY)
    assert parsed.full_name.status == "invalid"


def test_missing_fields(settings: Settings) -> None:
    parsed = parse_gplx_front([line("Độc lập Tự do Hạnh phúc")], settings, TODAY)
    assert parsed.id_number.status == "missing"
    assert parsed.full_name.status == "missing"
    assert parsed.dob.status == "missing"
    assert parsed.vehicle_class.status == "missing"
    assert parsed.expiry.status == "missing"
    assert parsed.found_labels == ()
