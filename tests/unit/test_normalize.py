import pytest

from ekyc.pipeline.normalize import fold


@pytest.mark.parametrize(
    "text,expected",
    [
        ("CĂN CƯỚC CÔNG DÂN", "can cuoc cong dan"),
        ("Đặc điểm nhận dạng", "dac diem nhan dang"),
        ("Driver’s License", "drivers license"),
        ("  A\n B\t C  ", "a b c"),
        ("`D'`", "d"),
        ("", ""),
    ],
)
def test_fold(text: str, expected: str) -> None:
    assert fold(text) == expected
    assert fold(text).isascii()


def test_fold_aligned() -> None:
    import unicodedata

    from ekyc.pipeline.normalize import fold_aligned

    for text in ("Đặc điểm", unicodedata.normalize("NFD", "Đặc điểm")):
        assert fold_aligned(text) == "dac diem"
        assert len(fold_aligned(text)) == len(unicodedata.normalize("NFC", text))
    assert fold_aligned("a\u200db") == "a b"
