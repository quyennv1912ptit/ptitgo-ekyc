import pytest

from ekyc.config import Settings
from ekyc.pipeline.normalize import fold
from ekyc.pipeline.parsers.cccd_labels import ENGLISH_SCRUB_LABELS, LABELS
from ekyc.pipeline.parsers.label_scrub import is_label_only, scrub_labels


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Place of ongin: Thanh Hoa", "Thanh Hoa"),
        ("/ Place ot residence:", ""),
        ("Dale of expiry", ""),
        ("Fullname: NGUYEN VAN A", "NGUYEN VAN A"),
        ("Place of origin: / Place of residence: Thanh Hoa", "Thanh Hoa"),
    ],
)
def test_scrub(settings: Settings, text: str, expected: str) -> None:
    found: list[str] = []
    assert scrub_labels(text, settings, found) == expected
    assert found
    assert is_label_only(text, settings) is (not expected)


@pytest.mark.parametrize(
    "text",
    [
        "Thôn Phúc Lộc, Xã Quảng Phú, Huyện Quảng Xương",
        "Số 12 Đường Nguyễn Trãi, Phường Thanh Xuân Trung",
        "Hòa Vang, Đà Nẵng",
        "Xã Phú Nam, Đồng Nai",
        "Phường Hà Đông, Hà Nội",
        "Đắk Lắk",
        "TP. Hồ Chí Minh",
        "Quê quán",
    ],
)
def test_no_place_corruption(settings: Settings, text: str) -> None:
    assert scrub_labels(text, settings) == text
    assert not is_label_only(text, settings)


def test_labels() -> None:
    for spec in LABELS.values():
        assert all(phrase == fold(phrase) for phrase in (*spec.phrases, *spec.sublabels))
    assert "sex" not in ENGLISH_SCRUB_LABELS and "expiry" not in ENGLISH_SCRUB_LABELS
    assert all(len(label) >= 8 for label in ENGLISH_SCRUB_LABELS)


def test_found_optional_and_nfc(settings: Settings) -> None:
    import unicodedata

    text = unicodedata.normalize("NFD", "Place of ongin: Thanh Hóa")
    assert scrub_labels(text, settings) == "Thanh Hóa"
    assert scrub_labels("", settings) == ""
    assert is_label_only(" : /", settings)
