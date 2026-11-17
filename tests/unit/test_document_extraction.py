import io
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PIL import Image

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.card_image import PreparedCard
from ekyc.pipeline.doc_types import DocClassification, DocType, Side
from ekyc.pipeline.ocr.base import OcrLine
from ekyc.services import document_extraction as module
from ekyc.services.document_extraction import DocumentExtractor


def encode(image=None):
    buffer = io.BytesIO()
    Image.fromarray(image).save(buffer, format="PNG") if image is not None else Image.new(
        "RGB", (240, 140)
    ).save(buffer, format="PNG")
    return buffer.getvalue()


def test_shared_stage_order_qr_survives_failed_warp_and_ocr(settings, monkeypatch):
    events = []
    load = module.load_image_from_bytes

    def validate(*args):
        events.append("validate")
        return load(*args)

    def qr(*args):
        events.append("qr")
        return "QR_FOUND_SUPPORTED", {"cccd_id": "000000000001"}

    def prepare(image):
        events.append("prepare")
        raise EkycError(ErrorCode.CARD_NOT_FOUND)

    def read(image):
        events.append("ocr")
        raise RuntimeError("synthetic")

    monkeypatch.setattr(module, "load_image_from_bytes", validate)
    monkeypatch.setattr(module, "decode_qr", qr)
    extractor = DocumentExtractor(
        settings, SimpleNamespace(prepare_loaded=prepare), SimpleNamespace(read_lines=read)
    )
    result = extractor.extract(encode(), "cccd_front")
    assert events == ["validate", "qr", "prepare", "ocr"]
    assert result.qr_status == "QR_FOUND_SUPPORTED"
    assert result.ocr_status == "OCR_FAILED"
    assert result.candidates[0].candidate_value == "000000000001"
    assert set(result.warnings) == {"CARD_NOT_FOUND", "RAW_FRAME_FALLBACK", "OCR_UNAVAILABLE"}


def test_unsupported_side_skips_detector_and_ocr(settings):
    result = DocumentExtractor(settings).extract(encode(), "gplx_back")
    assert result.ocr_status == "OCR_UNSUPPORTED"
    assert result.candidates == []


def test_orientation_compares_parsed_fields_and_retains_conflicting_candidates(
    settings, monkeypatch
):
    events = []
    image = np.zeros((140, 240, 3), dtype=np.uint8)
    image[0, 0] = 255

    def qr(*args):
        return "QR_FOUND_SUPPORTED", {"cccd_id": "000000000001"}

    def prepare(loaded):
        events.append("prepare")
        return PreparedCard(loaded.bgr)

    def ocr(frame):
        events.append("ocr")
        lines = [OcrLine("Số: 000000000002", 0.9, 5, 10, 100, 20)]
        if frame[0, 0, 0] == 0:
            lines.append(OcrLine("Họ và tên: TRẦN VĂN MẪU", 0.8, 5, 50, 210, 62))
        return lines

    def classify(*args):
        events.append("classify")
        return DocClassification(DocType.CCCD, Side.FRONT, 0.9, 0, [], True)

    monkeypatch.setattr(module, "decode_qr", qr)
    extractor = DocumentExtractor(
        settings,
        SimpleNamespace(prepare_loaded=prepare),
        SimpleNamespace(read_lines=ocr),
        SimpleNamespace(classify=classify),
    )
    result = extractor.extract(encode(image), "cccd_front")
    assert events == ["prepare", "ocr", "classify", "ocr", "classify"]
    assert result.conflict_fields == ["id_number"]
    assert {c.candidate_value for c in result.candidates if c.conflict} == {
        "000000000001",
        "000000000002",
    }
    assert any(c.field_key == "full_name" and c.parse_status == "parsed" for c in result.candidates)
    assert any(c.field_key == "dob" and c.parse_status == "missing" for c in result.candidates)
    assert "ORIENTATION_UNCERTAIN" not in result.warnings


def test_assisted_orientation_reports_tie(settings):
    frame = np.zeros((24, 40, 3), np.uint8)
    _, data = cv2.imencode(".png", frame)
    extractor = DocumentExtractor(
        settings,
        SimpleNamespace(prepare_loaded=lambda loaded: PreparedCard(loaded.bgr)),
        SimpleNamespace(read_lines=lambda _: []),
    )
    result = extractor.extract(data.tobytes(), "gplx_front", qr=False)
    assert result.ocr_status == "OCR_NO_TEXT"
    assert "ORIENTATION_UNCERTAIN" in result.warnings


def test_conflict_normalizes_unicode_case_and_spacing_without_removing_accents(settings):
    from ekyc.schemas.document import ExtractionResult
    from ekyc.services.document_extraction import make_candidate, record_conflicts

    evidence = ExtractionResult(
        slot="cccd_front",
        candidates=[
            make_candidate("cccd_front", "full_name", "Đỗ Ánh Mẫu", "QR"),
            make_candidate("cccd_front", "full_name", "  ĐỖ ÁNH   MẪU  ", "OCR"),
        ],
    )
    record_conflicts(evidence)
    assert evidence.conflict_fields == []
    evidence.candidates[1].candidate_value = "DO ANH MAU"
    record_conflicts(evidence)
    record_conflicts(evidence)
    assert evidence.conflict_fields == ["full_name"]
    assert all(item.warnings == ["QR_OCR_MISMATCH"] for item in evidence.candidates)


@pytest.mark.parametrize(
    ("case", "warning"),
    [
        ("no_box", "DETECTOR_NO_BOX"),
        ("bad_pose", "KEYPOINT_INVALID_GEOMETRY"),
        ("low_confidence", "KEYPOINT_CONFIDENCE_REJECTED"),
        ("warp", "CARD_WARP_FAILED"),
        ("quality", "CARD_QUALITY_REJECTED"),
    ],
)
def test_preparation_failure_diagnostics_distinguish_stages(settings, monkeypatch, case, warning):
    from ekyc.pipeline.card_detector import CardDetectionResult
    from ekyc.pipeline.card_image import DefaultCardPreparer
    from ekyc.pipeline.yolo_card_detector import CardDetection, DetectionOutcome, DetectionStatus

    prep = DefaultCardPreparer(settings)
    corners = np.float32([[10, 10], [220, 10], [220, 120], [10, 120]])
    if case in {"no_box", "bad_pose"}:
        outcome = DetectionOutcome(
            DetectionStatus.NOT_FOUND, None, 0, 1, 0, ErrorCode.CARD_NOT_FOUND, warning
        )
    else:
        scores = np.zeros(4, np.float32) if case == "low_confidence" else np.ones(4, np.float32)
        detection = CardDetection(corners, np.float32([10, 10, 220, 120]), 0.9, 0, scores)
        outcome = DetectionOutcome(DetectionStatus.DETECTED, detection, 0, 1, 0, None)

    prep.detector = SimpleNamespace(detect_cards=lambda _: outcome)
    if case == "warp":

        def fail_warp(*_):
            raise cv2.error("synthetic warp failure")

        monkeypatch.setattr("ekyc.pipeline.yolo_card_detector.rectify_corners", fail_warp)
    if case == "quality":

        def reject(_):
            raise EkycError(ErrorCode.IMAGE_BLURRY)

        data = np.zeros((100, 180, 3), np.uint8)
        monkeypatch.setattr(prep, "gate", lambda *_: ())
        monkeypatch.setattr(prep, "finish", reject)
        monkeypatch.setattr(
            "ekyc.pipeline.yolo_card_detector.rectify_corners",
            lambda *_: CardDetectionResult(data, corners, 180, 100, 1.8),
        )

    engine = SimpleNamespace(read_lines=lambda _: [])
    result = DocumentExtractor(settings, prep, engine).extract(encode(), "gplx_front", qr=False)
    assert warning in result.warnings
    assert "RAW_FRAME_FALLBACK" in result.warnings
