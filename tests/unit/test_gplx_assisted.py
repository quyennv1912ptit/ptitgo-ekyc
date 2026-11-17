from datetime import date

import pytest

from ekyc.pipeline.ocr.base import OcrLine
from ekyc.pipeline.parsers.gplx_assisted import parse_assisted_gplx
from ekyc.pipeline.parsers.gplx_parser import parse_gplx_front

TODAY = date(2026, 11, 12)


def line(text, x1=10, x2=140, y=10):
    return OcrLine(text, 0.9, x1, y, x2, y + 25)


def test_name_label_residue_uses_unique_neighbor_or_abstains(settings):
    anchor = line("Họ tên: Full nam")
    gt = "ĐỖ ÁNH MẪU"
    value = line("Đỗ Ánh Mẫu", 160, 340)
    assert parse_gplx_front([anchor, value], settings, TODAY).full_name.value == "FULL NAM"
    assert parse_assisted_gplx([anchor, value], settings, TODAY).full_name.value == gt
    other = line("TRẦN TÊN MẪU", 350, 450)
    parsed = parse_assisted_gplx([anchor, value, other], settings, TODAY)
    assert parsed.full_name.value is None
    assert parsed.conflicts == ("full_name",)


def test_dob_does_not_borrow_issue_date(settings):
    mixed = line("Ngày sinh: Ngày cấp: 21/02/2020")
    assert parse_gplx_front([mixed], settings, TODAY).dob.value == date(2020, 2, 21)
    assert parse_assisted_gplx([mixed], settings, TODAY).dob.value is None
    complete = line("Ngày sinh: 21/02/1990 Ngày cấp: 21/02/2020")
    assert parse_assisted_gplx([complete], settings, TODAY).dob.value == date(1990, 2, 21)


def test_expiry_keeps_explicit_indefinite_but_rejects_competing_date(settings):
    assert parse_assisted_gplx([line("Ngày hết hạn: Không thời hạn")], settings, TODAY).no_expiry
    mixed = line("Ngày hết hạn: Không thời hạn 01/03/2030")
    assert parse_gplx_front([mixed], settings, TODAY).no_expiry
    result = parse_assisted_gplx([mixed], settings, TODAY)
    assert not result.no_expiry and result.expiry.value is None
    assert "expiry" in result.conflicts
    assert not parse_assisted_gplx([], settings, TODAY).no_expiry


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A1, B2", ("A1", "B2")),
        ("A1; B2; A1", ("A1", "B2")),
        ("A1/X9", None),
        ("C/1", None),
        ("A1", "A1"),
    ],
)
def test_multi_class_requires_every_explicit_token_to_be_valid(settings, text, expected):
    records = [line(f"Hạng Class: {text}"), line("Số/No: 001234567890", y=65)]
    parsed = parse_assisted_gplx(records, settings, TODAY)
    assert parsed.vehicle_class.value == expected
    assert parsed.id_number.value == "001234567890"
    if text == "A1, B2":
        assert parse_gplx_front(records, settings, TODAY).vehicle_class.value is None


def test_assisted_parser_keeps_unicode_and_never_enables_in_production(settings, monkeypatch):
    from unicodedata import normalize

    from ekyc.services.document_extraction import DocumentExtractor

    records = [line("Hạng Class: A1, B2"), line(normalize("NFD", "Họ tên: Đỗ Ánh Mẫu"), y=50)]
    disabled = DocumentExtractor(settings).parse("gplx_front", records, TODAY)
    assert disabled.vehicle_class.value is None
    monkeypatch.setenv("EKYC_EXPERIMENTAL_GPLX_ASSOCIATION", "1")
    enabled = DocumentExtractor(settings)
    result = enabled.parse("gplx_front", records, TODAY)
    assert result.full_name.value == "ĐỖ ÁNH MẪU"
    assert next(
        c for c in enabled.candidates("gplx_front", result) if c.field_key == "vehicle_class"
    ).candidate_value == ["A1", "B2"]
    production = DocumentExtractor(settings.model_copy(update={"env": "prod"}))
    assert production.parse("gplx_front", records, TODAY).vehicle_class.value is None
