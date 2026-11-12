import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from threading import BoundedSemaphore
from time import monotonic
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.concurrency import job_slot
from ekyc.core.errors import EkycError, ErrorCode, WarningCode
from ekyc.pipeline.card_image import CardImagePreparer
from ekyc.pipeline.doc_classifier import DocClassifier
from ekyc.pipeline.doc_types import DocClassification, DocType, Side
from ekyc.pipeline.ocr.base import OcrEngine, OcrLine
from ekyc.pipeline.ocr.line_grouping import group_into_lines
from ekyc.pipeline.parsers.cccd_checks import (
    REVIEW_ORDER,
    check_id_structure,
    collect_review_fields,
)
from ekyc.pipeline.parsers.cccd_parser import Field, parse_cccd_back, parse_cccd_front
from ekyc.schemas.cccd import CccdData
from ekyc.schemas.common import Warning
from ekyc.services.doc_validation import ensure_document

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CccdResult:
    data: CccdData
    warnings: list[Warning]


@contextmanager
def image_errors(image: str) -> Iterator[None]:
    try:
        yield
    except EkycError as exc:
        details = [dict(item) for item in exc.details] if exc.details else [{}]
        details[0]["image"] = image
        raise EkycError(exc.code, exc.message, details=details) from exc


def resize_for_ocr(image: np.ndarray, max_side: int) -> np.ndarray:
    height, width = image.shape[:2]
    scale = min(1.0, max_side / max(height, width))
    if scale == 1:
        return image
    return cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


class CccdService:
    def __init__(
        self,
        settings: Settings,
        preparer: CardImagePreparer,
        ocr_engine: OcrEngine,
        classifier: DocClassifier,
        job_slots: BoundedSemaphore,
    ) -> None:
        self.settings, self.preparer = settings, preparer
        self.ocr_engine, self.classifier, self.job_slots = ocr_engine, classifier, job_slots

    def _read(self, image: np.ndarray, deadline: float) -> tuple[list[OcrLine], DocClassification]:
        resized = resize_for_ocr(image, self.settings.ocr_max_side_px)

        def attempt(img: np.ndarray) -> tuple[list[OcrLine], DocClassification]:
            boxes = self.ocr_engine.read_lines(img)
            if monotonic() > deadline:
                raise EkycError(ErrorCode.ENGINE_TIMEOUT)
            lines = group_into_lines(
                boxes,
                row_tolerance=self.settings.line_row_tolerance,
                gap_ratio=self.settings.line_gap_ratio,
            )
            return lines, self.classifier.classify(lines, img.shape[0])

        lines, result = attempt(resized)
        if result.doc_type == DocType.UNKNOWN and self.settings.ocr_retry_rotate_180:
            rotated_lines, rotated = attempt(cv2.rotate(resized, cv2.ROTATE_180))
            if (rotated.readable, rotated.score) > (result.readable, result.score):
                return rotated_lines, rotated
        return lines, result

    def run(self, front_url: str, back_url: str, *, today: date | None = None) -> CccdResult:
        with job_slot(self.job_slots, self.settings.job_slot_timeout_s):
            started = monotonic()
            deadline = started + self.settings.cccd_timeout_s
            today = (
                today
                if today is not None
                else datetime.now(ZoneInfo(self.settings.timezone)).date()
            )
            with image_errors("front"):
                front_card = self.preparer.prepare(front_url)
            with image_errors("back"):
                back_card = self.preparer.prepare(back_url)
            with image_errors("front"):
                front_lines, front_cls = self._read(front_card.image, deadline)
            with image_errors("back"):
                back_lines, back_cls = self._read(back_card.image, deadline)
            if (
                front_cls.doc_type == back_cls.doc_type == DocType.CCCD
                and front_cls.side == Side.BACK
                and back_cls.side == Side.FRONT
            ):
                front_lines, back_lines = back_lines, front_lines
                front_cls, back_cls = back_cls, front_cls
                logger.debug("cccd_sides_swapped")
            with image_errors("front"):
                ensure_document(front_cls, expected_type=DocType.CCCD, expected_side=Side.FRONT)
                front = parse_cccd_front(front_lines, self.settings, today)
            with image_errors("back"):
                ensure_document(back_cls, expected_type=DocType.CCCD, expected_side=Side.BACK)
                back = parse_cccd_back(back_lines, self.settings)
            required = {"idNumber": front.id_number, "fullName": front.full_name, "dob": front.dob}
            for status, code in (
                ("missing", ErrorCode.FIELD_MISSING),
                ("invalid", ErrorCode.INVALID_FIELD_FORMAT),
            ):
                affected = [name for name, field in required.items() if field.status == status]
                if affected:
                    raise EkycError(code, details=[{"image": "front", "fields": affected}])
            if front.expiry.value is not None and front.expiry.value < today:
                raise EkycError(ErrorCode.DOCUMENT_EXPIRED, details=[{"image": "front"}])
            warnings: list[Warning] = [*front_card.warnings, *back_card.warnings]
            optional = {
                "gender": (front.gender, "giới tính"),
                "placeOfOrigin": (front.origin, "quê quán"),
                "address": (front.address, "địa chỉ"),
                "expiryDate": (front.expiry, "ngày hết hạn"),
            }
            for name, (field, display) in optional.items():
                if field.status != "parsed":
                    warnings.append(
                        Warning(
                            code=WarningCode.FIELD_NOT_FOUND,
                            field=name,
                            message=f"Không đọc được {display}, hãy nhập thủ công.",
                        )
                    )
            all_fields: dict[str, Field] = {
                **required,
                **{key: value[0] for key, value in optional.items()},
                "issueDate": back.issue_date,
            }
            confidence = {
                name: field.conf
                for name, field in all_fields.items()
                if field.status == "parsed" and field.conf is not None
            }
            for name, conf in confidence.items():
                if conf < self.settings.ocr_field_warn_conf:
                    warnings.append(Warning(code=WarningCode.LOW_OCR_CONFIDENCE, field=name))
            mismatches = check_id_structure(
                front.id_number.value, front.dob.value, front.gender.value
            )
            review_fields = collect_review_fields(
                all_fields, self.settings, no_expiry=front.no_expiry, mismatches=mismatches
            )
            if not front.layout_reliable:
                warnings.append(
                    Warning(
                        code=WarningCode.LAYOUT_UNCERTAIN,
                        message="Không xác định được bố cục thẻ, hãy kiểm tra quê quán và địa chỉ.",
                    )
                )
                review = set(review_fields) | {"placeOfOrigin", "address"}
                review_fields = [name for name in REVIEW_ORDER if name in review]
            if mismatches:
                warnings.append(
                    Warning(
                        code=WarningCode.ID_STRUCTURE_MISMATCH,
                        field="idNumber",
                        message=(
                            "Số CCCD không khớp với ngày sinh/giới tính đọc được, "
                            "hãy kiểm tra lại các trường này."
                        ),
                    )
                )
            low_resolution = any(warning.code == WarningCode.LOW_RESOLUTION for warning in warnings)
            if low_resolution:
                parsed = {name for name, field in all_fields.items() if field.status == "parsed"}
                review = set(review_fields) | parsed
                review_fields = [name for name in REVIEW_ORDER if name in review]
            data = CccdData(
                id_number=front.id_number.value,
                full_name=front.full_name.value,
                dob=front.dob.value,
                gender=front.gender.value,
                address=front.address.value,
                place_of_origin=front.origin.value,
                expiry_date=front.expiry.value,
                no_expiry=front.no_expiry,
                issue_date=back.issue_date.value,
                field_confidence=confidence,
                review_fields=review_fields,
                review_required=bool(review_fields) or low_resolution,
            )
            logger.info(
                "doc_type=CCCD side=FRONT/BACK status=%s elapsed_s=%.3f",
                {name: field.status for name, field in all_fields.items()},
                monotonic() - started,
            )
            return CccdResult(data, warnings)
