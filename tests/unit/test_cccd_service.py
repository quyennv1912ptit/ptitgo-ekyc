from datetime import date
from threading import BoundedSemaphore

import numpy as np
import pytest

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode, WarningCode
from ekyc.pipeline.doc_classifier import DocClassifier
from ekyc.pipeline.ocr.base import OcrLine
from ekyc.services.cccd_service import CccdService, resize_for_ocr

TODAY = date(2026, 11, 10)


def lines(texts: list[str], conf: float = 0.9) -> list[OcrLine]:
    return [
        OcrLine(
            text,
            conf,
            40 if text.startswith("Có giá trị đến") else 380,
            20 + i * 35,
            900,
            40 + i * 35,
        )
        for i, text in enumerate(texts)
    ]


def front() -> list[OcrLine]:
    return lines(
        [
            "CĂN CƯỚC CÔNG DÂN",
            "079200123456",
            "Họ và tên: NGUYEN VAN A",
            "Ngày sinh: 01/05/2000",
            "Giới tính: Nam",
            "Quê quán: Hà Nội",
            "Nơi thường trú: Hà Nội",
            "Có giá trị đến: 01/05/2035",
        ]
    )


def back() -> list[OcrLine]:
    return lines(["Đặc điểm nhận dạng", "IDVNM01234", "Ngày 01/01/2020"])


class FakePreparer:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail: ErrorCode | None = None

    def prepare(self, url: str) -> np.ndarray:
        self.calls.append(url)
        if self.fail:
            raise EkycError(self.fail, details=[{"extra": "retained"}])
        image = np.zeros((600, 1000, 3), dtype=np.uint8)
        image[0, 0] = 1 if "front" in url else 2
        return image


class FakeOcr:
    def __init__(self) -> None:
        self.scenarios = {1: front(), 2: back()}
        self.calls = 0
        self.reverse = False

    def read_lines(self, image: np.ndarray) -> list[OcrLine]:
        self.calls += 1
        if self.reverse:
            marker = int(image[-1, -1, 0])
        else:
            marker = int(image[0, 0, 0])
        return self.scenarios.get(marker, lines(["Random unreadable document background text"]))


@pytest.fixture
def service(settings: Settings) -> CccdService:
    settings = settings.model_copy(update={"job_slot_timeout_s": 0})
    return CccdService(
        settings, FakePreparer(), FakeOcr(), DocClassifier(settings), BoundedSemaphore(1)
    )


def test_valid(service: CccdService) -> None:
    result = service.run("front", "back", today=TODAY)
    assert result.data.id_number == "079200123456"
    assert result.data.full_name == "NGUYEN VAN A"
    assert result.data.dob == date(2000, 5, 1)
    assert result.data.issue_date == date(2020, 1, 1)
    assert result.data.field_confidence["idNumber"] == 0.9
    assert not result.warnings
    assert service.preparer.calls == ["front", "back"]


def test_rotate_and_swap(service: CccdService) -> None:
    service.ocr_engine.reverse = True
    assert service.run("front", "back", today=TODAY).data.full_name == "NGUYEN VAN A"
    assert service.ocr_engine.calls == 4
    service.ocr_engine.reverse = False
    assert service.run("back", "front", today=TODAY).data.id_number == "079200123456"


@pytest.mark.parametrize(
    "scenario,image,code,fields",
    [
        ("gplx", "front", ErrorCode.WRONG_DOCUMENT_TYPE, None),
        ("wrong_back", "back", ErrorCode.WRONG_SIDE, None),
        ("missing_id", "front", ErrorCode.FIELD_MISSING, ["idNumber"]),
        ("missing_name", "front", ErrorCode.FIELD_MISSING, ["fullName"]),
        ("invalid_dob", "front", ErrorCode.INVALID_FIELD_FORMAT, ["dob"]),
        ("expired", "front", ErrorCode.DOCUMENT_EXPIRED, None),
    ],
)
def test_business_errors(
    service: CccdService, scenario: str, image: str, code: ErrorCode, fields: list[str] | None
) -> None:
    content = front()
    if scenario == "gplx":
        content = lines(["GIẤY PHÉP LÁI XE", "Hạng: B", "Ho va ten NGUYEN VAN A"])
    elif scenario == "wrong_back":
        service.ocr_engine.scenarios[2] = front()
    elif scenario == "missing_id":
        content.pop(1)
    elif scenario == "missing_name":
        content.pop(2)
    elif scenario == "invalid_dob":
        content[3] = lines(["Ngày sinh: bad date"])[0]
    elif scenario == "expired":
        content[-1] = lines(["Có giá trị đến: 01/01/2020"])[0]
    # Giữ thứ tự tọa độ sau khi thay nội dung.
    service.ocr_engine.scenarios[1] = lines([item.text for item in content])
    with pytest.raises(EkycError) as info:
        service.run("front", "back", today=TODAY)
    assert info.value.code == code
    assert info.value.details[0]["image"] == image
    if fields:
        assert info.value.details[0]["fields"] == fields
    assert service.job_slots.acquire(blocking=False)
    service.job_slots.release()


def test_warnings(service: CccdService) -> None:
    texts = [item.text for item in front() if not item.text.startswith("Nơi thường trú")]
    texts[3] = "Ngày sinh: 01/05/2001"
    service.ocr_engine.scenarios[1] = lines(texts, conf=0.5)
    result = service.run("front", "back", today=TODAY)
    assert any(
        w.code == WarningCode.FIELD_NOT_FOUND and w.field == "address" for w in result.warnings
    )
    assert any(w.code == WarningCode.LOW_OCR_CONFIDENCE for w in result.warnings)
    assert any(w.code == WarningCode.ID_STRUCTURE_MISMATCH for w in result.warnings)


def test_image_error_and_early_stop(service: CccdService) -> None:
    service.preparer.fail = ErrorCode.IMAGE_BLURRY
    with pytest.raises(EkycError) as info:
        service.run("front", "back", today=TODAY)
    assert info.value.code == ErrorCode.IMAGE_BLURRY
    assert info.value.details == [{"extra": "retained", "image": "front"}]
    assert service.preparer.calls == ["front"]


def test_timeout_and_busy(service: CccdService, monkeypatch: pytest.MonkeyPatch) -> None:
    import ekyc.services.cccd_service as module

    clock = iter([0.0, 26.0])
    monkeypatch.setattr(module, "monotonic", lambda: next(clock))
    with pytest.raises(EkycError) as info:
        service.run("front", "back", today=TODAY)
    assert info.value.code == ErrorCode.ENGINE_TIMEOUT
    assert info.value.details == [{"image": "front"}]
    service.job_slots.acquire()
    with pytest.raises(EkycError) as info:
        service.run("front", "back", today=TODAY)
    assert info.value.code == ErrorCode.ENGINE_BUSY
    service.job_slots.release()


def test_resize() -> None:
    image = np.zeros((400, 800, 3), dtype=np.uint8)
    assert resize_for_ocr(image, 1600) is image
    assert resize_for_ocr(image, 400).shape == (200, 400, 3)


def test_log_privacy(service: CccdService, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="ekyc.services.cccd_service"):
        service.run("front", "back", today=TODAY)
    assert "status=" in caplog.text
    assert "NGUYEN" not in caplog.text and "079200123456" not in caplog.text


def test_back_issue_date_optional(service: CccdService) -> None:
    service.ocr_engine.scenarios[2] = lines(
        ["Đặc điểm nhận dạng", "IDVNM01234", "Thông tin mẫu kiểm thử"]
    )
    assert service.run("front", "back", today=TODAY).data.issue_date is None


def test_service_reviews(service: CccdService) -> None:
    result = service.run("front", "back", today=TODAY)
    assert result.data.review_fields == [] and result.data.review_required is False
    service.ocr_engine.scenarios[2] = lines(
        ["Đặc điểm nhận dạng", "IDVNM01234", "Ngày 01/01/2020"], 0.46
    )
    result = service.run("front", "back", today=TODAY)
    assert result.data.review_fields == ["issueDate"] and result.data.review_required
    assert result.data.field_confidence["issueDate"] == 0.46
    assert any(
        w.code == WarningCode.LOW_OCR_CONFIDENCE and w.field == "issueDate" for w in result.warnings
    )


def test_service_mismatch_reviews(service: CccdService) -> None:
    content = [item.text for item in front()]
    content[3] = "Ngày sinh: 01/05/2001"
    service.ocr_engine.scenarios[1] = lines(content, 0.99)
    result = service.run("front", "back", today=TODAY)
    assert result.data.review_fields == ["idNumber", "dob"]


def test_service_no_expiry_reviews(service: CccdService) -> None:
    content = [item.text for item in front()]
    content[-1] = "Có giá trị đến: Không thời hạn"
    service.ocr_engine.scenarios[1] = lines(content, 0.5)
    result = service.run("front", "back", today=TODAY)
    assert "expiryDate" not in result.data.review_fields


def test_layout_uncertain_warning(service: CccdService) -> None:
    service.ocr_engine.scenarios[1] = [
        OcrLine(item.text, item.conf, 380, item.y1, item.x2, item.y2) for item in front()
    ]
    result = service.run("front", "back", today=TODAY)
    assert result.data.review_fields == ["placeOfOrigin", "address"]
    assert result.data.review_required
    warnings = [
        warning for warning in result.warnings if warning.code == WarningCode.LAYOUT_UNCERTAIN
    ]
    assert len(warnings) == 1 and warnings[0].field is None
    assert (
        warnings[0].message == "Không xác định được bố cục thẻ, hãy kiểm tra quê quán và địa chỉ."
    )
