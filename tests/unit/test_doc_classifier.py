import logging

import pytest

from ekyc.config import Settings
from ekyc.pipeline.doc_classifier import DocClassifier
from ekyc.pipeline.doc_types import DocType, Side
from ekyc.pipeline.ocr.base import OcrLine


def line(text: str, y_frac: float = 0.1, conf: float = 0.9, img_h: float = 1000) -> OcrLine:
    return OcrLine(text, conf, 0, y_frac * img_h - 5, 100, y_frac * img_h + 5)


def background() -> list[OcrLine]:
    return [line("Ho va ten Nguyen Van A", 0.6), line("Ngay sinh 01/01/2000", 0.7)]


@pytest.mark.parametrize(
    "texts,doc_type,side",
    [
        (["CĂN CƯỚC CÔNG DÂN", "Quê quán", "079123456789"], DocType.CCCD, Side.FRONT),
        (["CAN CUOC CONG OAN"], DocType.CCCD, Side.FRONT),
        (["Đặc điểm nhận dạng", "IDVNM01234"], DocType.CCCD, Side.BACK),
        (["GIẤY PHÉP LÁI XE", "hang/class: a1", "079123456789"], DocType.GPLX, Side.FRONT),
        (["CHỨNG MINH NHÂN DÂN"], DocType.CMND, Side.FRONT),
    ],
)
def test_classification(
    settings: Settings, texts: list[str], doc_type: DocType, side: Side
) -> None:
    classifier = DocClassifier(settings)
    lines = [line(text) for text in texts] + background()
    result = classifier.classify(lines, 1000)
    assert (result.doc_type, result.side) == (doc_type, side)
    assert result.score >= settings.doc_accept_score
    assert result == classifier.classify(lines, 1000)
    assert all(
        item.startswith(f"{doc_type.value.lower()}_{side.value.lower()}:")
        for item in result.evidence
    )
    assert all(
        item.split(":")[1]
        in {"title", "origin", "id12", "identification", "mrz", "issuer", "class", "number"}
        for item in result.evidence
    )


def test_random_readable(settings: Settings) -> None:
    result = DocClassifier(settings).classify(background(), 1000)
    assert result.readable
    assert result.doc_type == DocType.UNKNOWN


@pytest.mark.parametrize("lines", [[], [line("abc")], [line("   ")]])
def test_unreadable(settings: Settings, lines: list[OcrLine]) -> None:
    result = DocClassifier(settings).classify(lines, 1000)
    assert not result.readable
    assert result.doc_type == DocType.UNKNOWN and result.side == Side.UNKNOWN
    assert result.score == result.runner_up == 0.0
    assert result.evidence == []


@pytest.mark.parametrize(
    "text,y,conf,accepted",
    [
        ("CĂN CƯỚC CÔNG DÂN", 0.9, 0.9, False),
        ("CĂN CƯỚC CÔNG DÂN", 0.1, 0.9, True),
        ("cuoc", 0.1, 0.9, False),
        ("CĂN CƯỚC CÔNG DÂN", 0.1, 0.29, False),
    ],
)
def test_filters(settings: Settings, text: str, y: float, conf: float, accepted: bool) -> None:
    result = DocClassifier(settings).classify([line(text, y, conf)] + background(), 1000)
    assert (result.doc_type == DocType.CCCD) is accepted


def test_doc_type_margin(settings: Settings) -> None:
    result = DocClassifier(settings).classify(
        [line("CĂN CƯỚC CÔNG DÂN"), line("GIẤY PHÉP LÁI XE")] + background(), 1000
    )
    assert result.doc_type == DocType.UNKNOWN and result.side == Side.UNKNOWN
    assert result.score == result.runner_up == 1.0
    assert "cccd_front:title" in result.evidence


def test_side_margin(settings: Settings) -> None:
    result = DocClassifier(settings).classify(
        [line("CĂN CƯỚC CÔNG DÂN"), line("Đặc điểm nhận dạng")] + background(), 1000
    )
    assert result.doc_type == DocType.CCCD and result.side == Side.UNKNOWN
    assert result.runner_up == 0


def test_no_double_counting(settings: Settings) -> None:
    result = DocClassifier(settings).classify([line("Quê quán")] * 5 + background(), 1000)
    assert result.score == 0.4
    assert result.doc_type == DocType.UNKNOWN
    assert result.evidence == ["cccd_front:origin"]


def test_debug_private(settings: Settings, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="ekyc.pipeline.doc_classifier"):
        DocClassifier(settings).classify([line("CĂN CƯỚC CÔNG DÂN")] + background(), 1000)
    assert "cccd_front:title" in caplog.text
    assert "Nguyen" not in caplog.text and "CĂN CƯỚC" not in caplog.text


@pytest.mark.parametrize("height", [0, -1, float("nan"), float("inf")])
def test_invalid_height(settings: Settings, height: float) -> None:
    with pytest.raises(ValueError):
        DocClassifier(settings).classify([], height)


def test_ocr_line() -> None:
    assert line("x", 0.3).center_y == 300
    with pytest.raises(ValueError):
        line("x", conf=1.1)
