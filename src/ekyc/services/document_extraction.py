"""Shared document stages; strict and assisted callers choose their acceptance policy."""

import os
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.core.inference_metrics import stage
from ekyc.pipeline.card_image import DefaultCardPreparer, PreparedCard
from ekyc.pipeline.doc_classifier import DocClassifier
from ekyc.pipeline.doc_types import DocType, Side
from ekyc.pipeline.image_loader import load_image_from_bytes
from ekyc.pipeline.ocr.base import OcrEngine, OcrLine
from ekyc.pipeline.ocr.line_grouping import group_into_lines
from ekyc.pipeline.parsers.cccd_parser import parse_cccd_back, parse_cccd_front
from ekyc.pipeline.parsers.gplx_assisted import parse_assisted_gplx
from ekyc.pipeline.parsers.gplx_parser import parse_gplx_front
from ekyc.schemas.document import Candidate, DocumentSlot, ExtractionResult


def parse_qr(payload, slot):
    if not payload:
        return "QR_NOT_FOUND", {}
    if not slot.startswith("cccd") or len(payload) > 1024:
        return "QR_FOUND_UNSUPPORTED", {}
    parts = payload.split("|")
    if len(parts) != 7:
        return "QR_FOUND_UNSUPPORTED", {}
    number, old_number, name, birth, gender, address, issued = parts
    if not (
        re.fullmatch(r"[0-9]{12}", number)
        and re.fullmatch(r"(?:[0-9]{9})?", old_number)
        and 1 <= len(name) <= 150
        and all(c.isalpha() or c in " -'" for c in name)
        and gender in {"Nam", "Nữ"}
        and address.strip()
    ):
        return "QR_FOUND_UNSUPPORTED", {}
    try:
        if not all(re.fullmatch(r"[0-9]{8}", value) for value in (birth, issued)):
            raise ValueError
        dob = datetime.strptime(birth, "%d%m%Y").date().isoformat()
        datetime.strptime(issued, "%d%m%Y")
    except ValueError:
        return "QR_FOUND_UNSUPPORTED", {}
    return "QR_FOUND_SUPPORTED", {
        "cccd_id": number,
        "cccd_name": name,
        "cccd_dob": dob,
        "cccd_gender": "MALE" if gender == "Nam" else "FEMALE",
    }


def expiry_status(lines, parsed, cfg):
    from ekyc.pipeline.parsers.gplx_parser import _find_spans

    if parsed is not None:
        if parsed.no_expiry:
            return "NO_EXPIRY"
        if parsed.expiry.status == "parsed":
            return "READ"
        if parsed.expiry.status == "invalid":
            return "VALIDATION_FAILED"
    if any(span.key == "expiry" for line in lines for span in _find_spans(line.text, cfg)):
        return "PARSE_FAILED"
    return "OCR_MISSING"


def reading_views(image):
    if image.shape[0] > image.shape[1]:
        yield cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        yield cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        yield image
    else:
        yield image
        yield cv2.rotate(image, cv2.ROTATE_180)


def read_oriented(image, read):
    best = ("OCR_NO_TEXT", {})
    for frame in reading_views(image):
        status, fields = read(frame)
        if (len(fields), status == "OCR_NO_FIELDS") > (len(best[1]), best[0] == "OCR_NO_FIELDS"):
            best = status, fields
    return best


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


FIELD_MAP = {
    "cccd_front": {
        "id_number": "id_number",
        "full_name": "full_name",
        "dob": "dob",
        "gender": "gender",
        "place_of_origin": "origin",
        "address": "address",
        "expiry": "expiry",
    },
    "cccd_back": {"issue_date": "issue_date"},
    "gplx_front": {
        "id_number": "id_number",
        "full_name": "full_name",
        "dob": "dob",
        "vehicle_class": "vehicle_class",
        "expiry": "expiry",
    },
}
LEGACY_KEYS = {
    "cccd_id": "id_number",
    "cccd_name": "full_name",
    "cccd_dob": "dob",
    "cccd_gender": "gender",
    "gplx_number": "id_number",
    "gplx_name": "full_name",
    "gplx_dob": "dob",
    "gplx_classes": "vehicle_class",
    "gplx_expiry": "expiry",
}


def make_candidate(slot, key, value, source, status="parsed", confidence=None):
    document, side = slot.upper().split("_")
    return Candidate(
        field_key=key,
        candidate_value=value,
        source=source,
        source_document=document,
        source_side=side,
        engine_confidence=confidence,
        parse_status=status,
        validation_status={"parsed": "valid", "invalid": "invalid"}.get(status, "unknown"),
    )


def legacy_values(candidates, slot, source):
    values = {
        candidate.field_key: candidate.candidate_value
        for candidate in candidates
        if candidate.source == source.upper() and candidate.parse_status == "parsed"
    }
    return {
        key: values[field]
        for key, field in LEGACY_KEYS.items()
        if key.startswith(slot.split("_")[0] + "_") and field in values
    }


def ocr_status(raw, candidates):
    if not raw:
        return "OCR_NO_TEXT"
    return (
        "OCR_READ" if any(item.parse_status == "parsed" for item in candidates) else "OCR_NO_FIELDS"
    )


def decode_qr(image, slot):
    detector = cv2.QRCodeDetector()
    found_points = False
    for frame in reading_views(resize_for_ocr(image, 2500)):
        try:
            payload, points, _ = detector.detectAndDecode(frame)
        except cv2.error:
            return "QR_DECODE_FAILED", {}
        if payload:
            return parse_qr(payload, slot)
        found_points |= points is not None
    return ("QR_DECODE_FAILED" if found_points else "QR_NOT_FOUND"), {}


def same_field_value(key, left, right):
    if (
        key in {"full_name", "cccd_name", "gplx_name"}
        and isinstance(left, str)
        and isinstance(right, str)
    ):
        normalized_left = " ".join(unicodedata.normalize("NFC", left).lower().split())
        normalized_right = " ".join(unicodedata.normalize("NFC", right).lower().split())
        return normalized_left == normalized_right
    return left == right


def record_conflicts(result):
    grouped = {}
    for item in result.candidates:
        if item.validation_status == "valid":
            grouped.setdefault(item.field_key, []).append(item)
    for key, items in grouped.items():
        if any(
            not same_field_value(key, item.candidate_value, items[0].candidate_value)
            for item in items[1:]
        ):
            if key not in result.conflict_fields:
                result.conflict_fields.append(key)
            for item in items:
                item.conflict = True
                if "QR_OCR_MISMATCH" not in item.warnings:
                    item.warnings.append("QR_OCR_MISMATCH")


PREPARATION_REASONS = {
    "DETECTOR_NO_BOX",
    "DETECTOR_CARD_NOT_FOUND",
    "KEYPOINT_INVALID_GEOMETRY",
    "KEYPOINT_CONFIDENCE_REJECTED",
    "CARD_WARP_FAILED",
    "CARD_RECTIFICATION_REJECTED",
    "CARD_GEOMETRY_REJECTED",
    "CARD_QUALITY_REJECTED",
}


class DocumentExtractor:
    def __init__(
        self,
        settings: Settings,
        preparer=None,
        engine: OcrEngine | None = None,
        classifier: DocClassifier | None = None,
        *,
        engine_factory: Callable[[Settings], OcrEngine] | None = None,
    ):
        self.settings = settings
        self.preparer = preparer
        self.engine = engine
        self.classifier = classifier or DocClassifier(settings)
        self.engine_factory = engine_factory
        self.experimental_gplx = (
            not settings.deployed and os.environ.get("EKYC_EXPERIMENTAL_GPLX_ASSOCIATION") == "1"
        )

    def _preparer(self):
        if self.preparer is None:
            with stage("detector_init_ms"):
                self.preparer = DefaultCardPreparer(self.settings)
        return self.preparer

    def prepare_url(self, url: str) -> PreparedCard:
        return self._preparer().prepare(url)

    def prepare_native_card(self, loaded) -> PreparedCard:
        return self._preparer().prepare_loaded(loaded, native=True)

    def _engine(self) -> OcrEngine:
        if self.engine is None:
            if self.engine_factory is None:
                raise EkycError(ErrorCode.INTERNAL_ERROR)
            with stage("ocr_init_ms"):
                self.engine = self.engine_factory(self.settings)
        return self.engine

    def warm_up(self) -> None:
        self._preparer()
        engine = self._engine()
        if callable(warm := getattr(engine, "warm_up", None)):
            with stage("ocr_warm_up_ms"):
                warm()

    def read_text(self, image: np.ndarray) -> tuple[list[OcrLine], list[OcrLine]]:
        engine = self._engine()
        with stage("ocr_ms"):
            raw = engine.read_lines(image)
        lines = group_into_lines(
            raw,
            row_tolerance=self.settings.line_row_tolerance,
            gap_ratio=self.settings.line_gap_ratio,
        )
        return raw, lines

    def recognize(self, image, check: Callable[[], None]):
        check()
        raw, lines = self.read_text(image)
        check()
        return raw, lines, self.classifier.classify(lines, image.shape[0])

    def read_strict(self, image, check: Callable[[], None]):
        """Retain the legacy CCCD orientation policy and classification output."""
        image = resize_for_ocr(image, self.settings.ocr_max_side_px)
        _, lines, classification = self.recognize(image, check)
        if classification.doc_type == DocType.UNKNOWN and self.settings.ocr_retry_rotate_180:
            _, rotated, other = self.recognize(cv2.rotate(image, cv2.ROTATE_180), check)
            if (other.readable, other.score) > (classification.readable, classification.score):
                return rotated, other
        return lines, classification

    def parse(self, slot, lines, today):
        with stage("parser_ms"):
            if slot == "cccd_front":
                return parse_cccd_front(lines, self.settings, today)
            if slot == "cccd_back":
                return parse_cccd_back(lines, self.settings)
            if slot == "gplx_front":
                if self.experimental_gplx:
                    return parse_assisted_gplx(lines, self.settings, today)
                return parse_gplx_front(lines, self.settings, today)
            raise ValueError("Unsupported OCR layout")

    def candidates(self, slot, parsed):
        candidates = []
        for key, attr in FIELD_MAP[slot].items():
            field = getattr(parsed, attr)
            status, value, confidence = field.status, field.value, field.conf
            if key == "expiry" and parsed.no_expiry:
                status, value, confidence = "parsed", {"no_expiry": True, "expiry_date": None}, None
            elif status == "parsed" and value is not None:
                if key == "expiry":
                    value = {"no_expiry": False, "expiry_date": str(value)}
                elif key == "vehicle_class":
                    value = list(value) if isinstance(value, tuple) else [str(value)]
                else:
                    value = str(value)
            else:
                value = None
            candidate = make_candidate(slot, key, value, "OCR", status, confidence)
            if key in getattr(parsed, "conflicts", ()):
                candidate.conflict = True
                candidate.warnings.append("OCR_FIELD_CONFLICT")
            candidates.append(candidate)
        return candidates

    def _ocr(self, image, slot, result, check):
        expected_type, expected_side = slot.upper().split("_")
        today = datetime.now(ZoneInfo(self.settings.timezone)).date()
        ranked = []
        for frame in reading_views(resize_for_ocr(image, self.settings.ocr_max_side_px)):
            raw, lines, cls = self.recognize(frame, check)
            mismatch = cls.doc_type not in (expected_type, DocType.UNKNOWN) or cls.side not in (
                expected_side,
                Side.UNKNOWN,
            )
            if mismatch:
                continue
            parsed = self.parse(slot, lines, today)
            candidates = self.candidates(slot, parsed)
            eligible = sum(item.parse_status == "parsed" for item in candidates)
            rank = (
                cls.doc_type == expected_type and cls.side == expected_side,
                eligible,
                cls.score,
                bool(raw),
            )
            ranked.append((rank, candidates, raw, lines, parsed))
        if not ranked:
            result.ocr_status = "OCR_NO_FIELDS"
            result.warnings.append("DOCUMENT_SLOT_MISMATCH")
            return
        ranked.sort(key=lambda attempt: attempt[0], reverse=True)
        rank, candidates, raw, lines, parsed = ranked[0]
        if not rank[0] or (len(ranked) > 1 and rank == ranked[1][0]):
            result.warnings.append("ORIENTATION_UNCERTAIN")
            for item in candidates:
                item.warnings.append("ORIENTATION_UNCERTAIN")
        result.candidates.extend(candidates)
        result.ocr_status = ocr_status(raw, candidates)
        if slot == "gplx_front":
            result.field_statuses["gplx_expiry"] = expiry_status(
                [*raw, *lines], parsed, self.settings
            )

    def extract(
        self,
        data: bytes,
        slot: DocumentSlot,
        *,
        qr: bool = True,
        ocr: bool = True,
        check: Callable[[], None] = lambda: None,
        on_partial: Callable[[ExtractionResult], None] | None = None,
    ) -> ExtractionResult:
        """Assisted policy. Independent source observations are kept for review."""
        check()
        with stage("decode_ms"):
            loaded = load_image_from_bytes(data, self.settings)
        result = ExtractionResult(slot=slot)
        if qr:
            with stage("qr_ms"):
                result.qr_status, values = decode_qr(loaded.bgr, slot)
            result.candidates.extend(
                make_candidate(slot, LEGACY_KEYS[key], value, "QR") for key, value in values.items()
            )
            if on_partial is not None and ocr and slot in FIELD_MAP and result.candidates:
                partial_result = result.model_copy(deep=True)
                partial_result.ocr_status = "OCR_FAILED"
                partial_result.warnings.append("OCR_UNAVAILABLE")
                on_partial(partial_result)
        if ocr and slot not in FIELD_MAP:
            result.ocr_status = "OCR_UNSUPPORTED"
            result.field_statuses["gplx_expiry"] = "UNSUPPORTED"
        elif ocr:
            check()
            try:
                preparer = self._preparer()
                prepared = (
                    preparer.prepare_loaded(loaded, diagnostics=True)
                    if isinstance(preparer, DefaultCardPreparer)
                    else preparer.prepare_loaded(loaded)
                )
                image = prepared.image
                result.warnings.extend(warning.code.value for warning in prepared.warnings)
            except EkycError as exc:
                if exc.code == ErrorCode.ENGINE_TIMEOUT:
                    raise
                image = loaded.bgr
                result.warnings.append(exc.code.value)
                result.warnings.extend(
                    item["reason"]
                    for item in (exc.details or ())
                    if item.get("reason") in PREPARATION_REASONS
                )
                result.warnings.append("RAW_FRAME_FALLBACK")
            try:
                self._ocr(image, slot, result, check)
            except EkycError as exc:
                if exc.code == ErrorCode.ENGINE_TIMEOUT:
                    raise
                result.ocr_status = "OCR_FAILED"
                result.warnings.append("OCR_UNAVAILABLE")
            except (OSError, RuntimeError, cv2.error):
                result.ocr_status = "OCR_FAILED"
                result.warnings.append("OCR_UNAVAILABLE")
        check()
        for item in result.candidates:
            if item.conflict and item.field_key not in result.conflict_fields:
                result.conflict_fields.append(item.field_key)
            if item.source == "OCR":
                item.warnings = list(dict.fromkeys([*item.warnings, *result.warnings]))
        record_conflicts(result)
        return result
