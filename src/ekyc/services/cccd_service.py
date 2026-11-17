import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from threading import BoundedSemaphore
from time import monotonic
from zoneinfo import ZoneInfo

import numpy as np

from ekyc.config import Settings
from ekyc.core.concurrency import job_slot
from ekyc.core.errors import EkycError, ErrorCode, WarningCode
from ekyc.pipeline.card_image import CardImagePreparer
from ekyc.pipeline.doc_classifier import DocClassifier
from ekyc.pipeline.doc_types import DocClassification, DocType, Side
from ekyc.pipeline.ocr.base import OcrEngine, OcrLine
from ekyc.pipeline.parsers.cccd_checks import (
    REVIEW_ORDER,
    check_id_structure,
    collect_review_fields,
)
from ekyc.pipeline.parsers.cccd_parser import Field
from ekyc.schemas.cccd import CccdData
from ekyc.schemas.common import Warning
from ekyc.services.doc_validation import ensure_document
from ekyc.services.document_extraction import DocumentExtractor
from ekyc.services.document_extraction import resize_for_ocr as resize_for_ocr

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


def check_deadline(deadline: float) -> None:
    if monotonic() >= deadline:
        raise EkycError(ErrorCode.ENGINE_TIMEOUT)


class CccdService:
    def __init__(
        self,
        settings: Settings,
        preparer: CardImagePreparer | None,
        ocr_engine: OcrEngine | None,
        classifier: DocClassifier,
        job_slots: BoundedSemaphore,
    ) -> None:
        self.settings, self.preparer = settings, preparer
        self.ocr_engine, self.classifier, self.job_slots = ocr_engine, classifier, job_slots
        self.extraction = DocumentExtractor(settings, preparer, ocr_engine, classifier)

    def _read(self, image: np.ndarray, deadline: float) -> tuple[list[OcrLine], DocClassification]:
        def check() -> None:
            check_deadline(deadline)

        return self.extraction.read_strict(image, check)

    def run(
        self,
        front_url: str,
        back_url: str,
        *,
        today: date | None = None,
        deadline: float | None = None,
    ) -> CccdResult:
        with job_slot(self.job_slots, self.settings.job_slot_timeout_s):
            started = monotonic()
            if deadline is None:
                deadline = started + self.settings.cccd_timeout_s
            today = (
                today
                if today is not None
                else datetime.now(ZoneInfo(self.settings.timezone)).date()
            )
            with image_errors("front"):
                check_deadline(deadline)
                front_card = self.extraction.prepare_url(front_url)
            with image_errors("back"):
                check_deadline(deadline)
                back_card = self.extraction.prepare_url(back_url)
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
                check_deadline(deadline)
                ensure_document(front_cls, expected_type=DocType.CCCD, expected_side=Side.FRONT)
                front = self.extraction.parse("cccd_front", front_lines, today)
            with image_errors("back"):
                check_deadline(deadline)
                ensure_document(back_cls, expected_type=DocType.CCCD, expected_side=Side.BACK)
                back = self.extraction.parse("cccd_back", back_lines, today)
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
            image_review = any(
                warning.code
                in (
                    WarningCode.LOW_RESOLUTION,
                    WarningCode.LOW_KEYPOINT_CONFIDENCE,
                    WarningCode.CARD_GEOMETRY_UNCERTAIN,
                )
                for warning in warnings
            )
            if image_review:
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
                review_required=bool(review_fields) or image_review,
            )
            check_deadline(deadline)
            logger.info(
                "doc_type=CCCD side=FRONT/BACK status=%s elapsed_s=%.3f",
                {name: field.status for name, field in all_fields.items()},
                monotonic() - started,
            )
            return CccdResult(data, warnings)
