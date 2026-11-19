"""CCCD / Can Cuoc QR decoding, format parsing, and QR-vs-OCR cross-validation."""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import cv2
import numpy as np

QrStatus = Literal[
    "NOT_FOUND",
    "DETECTED_UNREADABLE",
    "DECODED_VALID",
    "INVALID_FORMAT",
    "UNSUPPORTED_FORMAT",
    "CONFLICT",
    "ENGINE_UNAVAILABLE",
]
QrSource = Literal["QR_IMAGE", "QR_LIVE_SCAN"]
DocumentSide = Literal["FRONT", "BACK", "UNKNOWN"]
CardVariant = Literal["CCCD_2021", "CAN_CUOC_2024", "GPLX_PET", "UNKNOWN"]
CrossValidationStatus = Literal[
    "MATCHED",
    "CONFLICT",
    "INSUFFICIENT_EVIDENCE",
    "NOT_APPLICABLE",
]

MAX_QR_PAYLOAD_LENGTH = 1024
ID12_RE = re.compile(r"^[0-9]{12}$")
OLD_ID_RE = re.compile(r"^(?:[0-9]{8,12})?$")
GPLX_ID_RE = re.compile(r"^(?=.*\d)[A-Z0-9]{6,20}$")
GPLX_CLASS_RE = re.compile(r"^[A-Z0-9]{1,8}$")
DATE_FORMATS = ("%d%m%Y", "%d/%m/%Y", "%d-%m-%Y")

# Normalized (x1, y1, x2, y2) QR ROIs on landscape card crops.
# CCCD 2021 has QR on front top-right; Can Cuoc 2024 has QR on back middle/upper-right.
# GPLX PET (from 06/2020) places QR on back left (plus mirrored 180-deg fallback).
CCCD_2021_FRONT_ROIS: tuple[tuple[float, float, float, float], ...] = (
    (0.66, 0.02, 0.99, 0.48),
    (0.01, 0.52, 0.34, 0.98),
)
CAN_CUOC_2024_BACK_ROIS: tuple[tuple[float, float, float, float], ...] = (
    (0.52, 0.04, 0.99, 0.72),
    (0.01, 0.28, 0.48, 0.96),
)
GPLX_BACK_ROIS: tuple[tuple[float, float, float, float], ...] = (
    (0.01, 0.02, 0.46, 0.78),
    (0.54, 0.22, 0.99, 0.98),
)
RAW_QUADRANT_ROIS: tuple[tuple[float, float, float, float], ...] = (
    (0.45, 0.00, 1.00, 0.55),
    (0.00, 0.45, 0.55, 1.00),
    (0.00, 0.00, 0.55, 0.55),
    (0.45, 0.45, 1.00, 1.00),
)


@dataclass(frozen=True)
class QrParsedResult:
    status: QrStatus
    legacy_status: str
    fields: dict[str, Any] = field(default_factory=dict)
    legacy_values: dict[str, str] = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class QrDecodeResult:
    parsed: QrParsedResult
    card_variant: CardVariant = "UNKNOWN"
    document_side: DocumentSide = "UNKNOWN"


def slot_to_side(slot: str | None) -> DocumentSide:
    if not slot:
        return "UNKNOWN"
    upper = slot.upper()
    if upper.endswith("_FRONT") or upper == "FRONT":
        return "FRONT"
    if upper.endswith("_BACK") or upper == "BACK":
        return "BACK"
    return "UNKNOWN"


def normalize_qr_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def parse_qr_date(value: str) -> date | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _looks_like_date_token(value: str) -> bool:
    return parse_qr_date(value) is not None


def _valid_person_name(name: str) -> bool:
    if not (1 <= len(name) <= 150):
        return False
    if not any(ch.isalpha() for ch in name):
        return False
    return all(ch.isalpha() or ch in " -'." for ch in name)


def _parse_gender(value: str) -> str | None:
    normalized = normalize_qr_text(value).casefold()
    if not normalized:
        return None
    if normalized == "nam":
        return "MALE"
    if normalized in {"nữ", "nu"}:
        return "FEMALE"
    return "__INVALID__"


def _default_today() -> date:
    return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()


def parse_cccd_qr_payload(
    payload: str | None,
    slot: str = "cccd_front",
    *,
    today: date | None = None,
) -> QrParsedResult:
    """Parse CCCD 2021 and Can Cuoc 2024 QR payloads without hardcoding 7 fields.

    Extracts only business fields needed (`id_number`, `full_name`, `dob`, `gender`,
    `issue_date`, `address`) and discards unnecessary personal fields such as old
    CMND number, canceled IDs, or parent names.
    """
    if not payload or not payload.strip():
        return QrParsedResult(status="NOT_FOUND", legacy_status="QR_NOT_FOUND")
    if not slot.startswith("cccd"):
        return QrParsedResult(status="UNSUPPORTED_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")

    raw = payload.strip()
    if len(raw) > MAX_QR_PAYLOAD_LENGTH:
        looks_cccd = "|" in raw and bool(ID12_RE.fullmatch(raw.split("|", 1)[0].strip()))
        return QrParsedResult(
            status="INVALID_FORMAT" if looks_cccd else "UNSUPPORTED_FORMAT",
            legacy_status="QR_FOUND_UNSUPPORTED",
        )

    parts = [normalize_qr_text(part) for part in raw.split("|")]
    if len(parts) < 5:
        if len(parts) >= 2 and ID12_RE.fullmatch(parts[0]):
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        return QrParsedResult(status="UNSUPPORTED_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")

    if len(parts) > 12:
        return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")

    id_number = parts[0]
    if not ID12_RE.fullmatch(id_number):
        return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")

    # Determine whether slot 1 is old_cmnd (7+ field layout) or full_name (6+ field layout).
    if len(parts) >= 7 and (parts[1] == "" or OLD_ID_RE.fullmatch(parts[1])):
        raw_name = parts[2]
        raw_dob = parts[3]
        raw_gender = parts[4]
        raw_address = parts[5]
        raw_issue = parts[6]
    elif len(parts) == 6 and (parts[1] == "" or OLD_ID_RE.fullmatch(parts[1])) and (
        parts[3] == "" or _looks_like_date_token(parts[3])
    ):
        raw_name = parts[2]
        raw_dob = parts[3]
        raw_gender = parts[4]
        raw_address = parts[5]
        raw_issue = ""
    elif not parts[1].isdigit() and (parts[2] == "" or _looks_like_date_token(parts[2])):
        raw_name = parts[1]
        raw_dob = parts[2]
        raw_gender = parts[3]
        raw_address = parts[4]
        raw_issue = parts[5] if len(parts) > 5 else ""
    elif len(parts) >= 7 and OLD_ID_RE.fullmatch(parts[1]) is None:
        return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
    else:
        return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")

    today = today or _default_today()
    fields: dict[str, Any] = {"id_number": id_number}
    legacy_values: dict[str, str] = {"cccd_id": id_number}
    missing: list[str] = []

    if raw_name:
        if not _valid_person_name(raw_name):
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        fields["full_name"] = raw_name
        legacy_values["cccd_name"] = raw_name
    else:
        missing.append("full_name")

    parsed_dob: date | None = None
    if raw_dob:
        parsed_dob = parse_qr_date(raw_dob)
        if parsed_dob is None or parsed_dob.year < 1900 or parsed_dob > today:
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        fields["dob"] = parsed_dob.isoformat()
        legacy_values["cccd_dob"] = parsed_dob.isoformat()
    else:
        missing.append("dob")

    if not fields.get("full_name") and not fields.get("dob"):
        return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")

    if raw_gender:
        gender = _parse_gender(raw_gender)
        if gender == "__INVALID__" or gender is None:
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        fields["gender"] = gender
        legacy_values["cccd_gender"] = gender
    else:
        missing.append("gender")

    if raw_address:
        if len(raw_address) > 300:
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        fields["address"] = raw_address
        legacy_values["cccd_address"] = raw_address
    else:
        missing.append("address")

    if raw_issue:
        parsed_issue = parse_qr_date(raw_issue)
        # Note: citizens under 14 can hold Can Cuoc 2024, so only require issue_date >= dob.
        if (
            parsed_issue is None
            or parsed_issue > today
            or (parsed_dob is not None and parsed_issue < parsed_dob)
        ):
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        fields["issue_date"] = parsed_issue.isoformat()
        legacy_values["cccd_issue_date"] = parsed_issue.isoformat()
    else:
        missing.append("issue_date")

    warnings = ("QR_PARTIAL_FIELDS",) if missing else ()
    return QrParsedResult(
        status="DECODED_VALID",
        legacy_status="QR_FOUND_SUPPORTED",
        fields=fields,
        legacy_values=legacy_values,
        missing_fields=tuple(missing),
        warnings=warnings,
    )


def _normalize_gplx_classes(raw: str) -> list[str] | None:
    tokens = [
        tok.strip().upper()
        for tok in re.split(r"[,;/\s]+", normalize_qr_text(raw))
        if tok.strip()
    ]
    if not tokens or len(tokens) > 20:
        return None
    if any(not GPLX_CLASS_RE.fullmatch(tok) for tok in tokens):
        return None
    return list(dict.fromkeys(tokens))


def parse_gplx_qr_payload(
    payload: str | None,
    slot: str = "gplx_back",
    *,
    today: date | None = None,
) -> QrParsedResult:
    """Parse Vietnamese GPLX PET QR payloads (pipe-delimited or labeled multi-line).

    Supports both 4+ field payloads (`Họ tên|Ngày sinh|Hạng|Nơi cấp`) and payloads
    starting with driver license number (`Số GPLX|Họ tên|Ngày sinh|Hạng|Nơi cấp`).
    Lookup URLs without embedded identity fields (e.g. `https://gplx.gov.vn/...`)
    return UNSUPPORTED_FORMAT without fabricating fields or following the URL.
    """
    if not payload or not payload.strip():
        return QrParsedResult(status="NOT_FOUND", legacy_status="QR_NOT_FOUND")
    if not slot.startswith("gplx"):
        return QrParsedResult(status="UNSUPPORTED_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")

    raw = payload.strip()
    lower = raw.casefold()
    if lower.startswith(("http://", "https://")):
        return QrParsedResult(status="UNSUPPORTED_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
    if len(raw) > MAX_QR_PAYLOAD_LENGTH:
        return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")

    today = today or _default_today()
    raw_id = ""
    raw_name = ""
    raw_dob = ""
    raw_classes = ""
    raw_expiry = ""

    if "|" in raw:
        parts = [normalize_qr_text(part) for part in raw.split("|")]
        if len(parts) < 4:
            return QrParsedResult(status="UNSUPPORTED_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        if len(parts) > 10:
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        if parts[0] and GPLX_ID_RE.fullmatch(parts[0].upper()):
            raw_id = parts[0].upper()
            raw_name = parts[1]
            raw_dob = parts[2]
            raw_classes = parts[3]
            if len(parts) >= 6 and parse_qr_date(parts[5]) is not None:
                raw_expiry = parts[5]
        else:
            raw_name = parts[0]
            raw_dob = parts[1]
            raw_classes = parts[2]
            if len(parts) >= 5 and parse_qr_date(parts[4]) is not None:
                raw_expiry = parts[4]
    elif "\n" in raw or "\r" in raw:
        lines = [normalize_qr_text(line) for line in raw.splitlines() if normalize_qr_text(line)]
        if len(lines) < 3:
            return QrParsedResult(status="UNSUPPORTED_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        labeled = False
        unlabeled: list[str] = []
        for line in lines:
            if ":" in line:
                label, val = [normalize_qr_text(item) for item in line.split(":", 1)]
                folded = fold_diacritics(label).casefold()
                if any(k in folded for k in ("so gplx", "so giay phep", "ma so")):
                    labeled = True
                    raw_id = val.upper()
                    continue
                if any(k in folded for k in ("ho ten", "ho va ten", "ten")):
                    labeled = True
                    raw_name = val
                    continue
                if any(k in folded for k in ("ngay sinh", "sinh")):
                    labeled = True
                    raw_dob = val
                    continue
                if "hang" in folded:
                    labeled = True
                    raw_classes = val
                    continue
                if any(k in folded for k in ("het han", "gia tri den", "co gia tri")):
                    labeled = True
                    raw_expiry = val
                    continue
                if any(k in folded for k in ("noi cap", "co quan cap", "don vi cap")):
                    labeled = True
                    continue
            unlabeled.append(line)
        if not labeled:
            if len(unlabeled) >= 4 and GPLX_ID_RE.fullmatch(unlabeled[0].upper()):
                raw_id = unlabeled[0].upper()
                raw_name = unlabeled[1]
                raw_dob = unlabeled[2]
                raw_classes = unlabeled[3]
            elif len(unlabeled) >= 3:
                raw_name = unlabeled[0]
                raw_dob = unlabeled[1]
                raw_classes = unlabeled[2]
    else:
        return QrParsedResult(status="UNSUPPORTED_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")

    fields: dict[str, Any] = {}
    legacy_values: dict[str, str] = {}
    missing: list[str] = []

    if raw_id:
        if not GPLX_ID_RE.fullmatch(raw_id):
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        fields["id_number"] = raw_id
        legacy_values["gplx_number"] = raw_id
    else:
        missing.append("id_number")

    if raw_name:
        if not _valid_person_name(raw_name):
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        fields["full_name"] = raw_name
        legacy_values["gplx_name"] = raw_name
    else:
        missing.append("full_name")

    if raw_dob:
        parsed_dob = parse_qr_date(raw_dob)
        if parsed_dob is None or parsed_dob.year < 1900 or parsed_dob > today:
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        fields["dob"] = parsed_dob.isoformat()
        legacy_values["gplx_dob"] = parsed_dob.isoformat()
    else:
        missing.append("dob")

    if raw_classes:
        classes = _normalize_gplx_classes(raw_classes)
        if not classes:
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        fields["vehicle_class"] = classes
        legacy_values["gplx_classes"] = ", ".join(classes)
    else:
        missing.append("vehicle_class")

    if raw_expiry:
        parsed_expiry = parse_qr_date(raw_expiry)
        if parsed_expiry is None:
            return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")
        fields["expiry"] = parsed_expiry.isoformat()
        legacy_values["gplx_expiry"] = parsed_expiry.isoformat()

    if "full_name" not in fields or (
        "dob" not in fields and "vehicle_class" not in fields and "id_number" not in fields
    ):
        return QrParsedResult(status="INVALID_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED")

    warnings = ("QR_PARTIAL_FIELDS",) if missing else ()
    return QrParsedResult(
        status="DECODED_VALID",
        legacy_status="QR_FOUND_SUPPORTED",
        fields=fields,
        legacy_values=legacy_values,
        missing_fields=tuple(missing),
        warnings=warnings,
    )


def parse_qr_payload(
    payload: str | None,
    slot: str = "cccd_front",
    *,
    today: date | None = None,
) -> QrParsedResult:
    if slot.startswith("gplx"):
        return parse_gplx_qr_payload(payload, slot=slot, today=today)
    return parse_cccd_qr_payload(payload, slot=slot, today=today)


def _pad_quiet_zone(image: np.ndarray, pad: int = 24) -> np.ndarray:
    value = (255, 255, 255) if image.ndim == 3 else 255
    return cv2.copyMakeBorder(image, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=value)


def _upscale_min_side(image: np.ndarray, target_min_side: int = 380) -> np.ndarray:
    height, width = image.shape[:2]
    min_side = min(height, width)
    if min_side <= 0 or min_side >= target_min_side:
        return image
    scale = min(3.0, target_min_side / min_side)
    return cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_CUBIC,
    )


def _crop_normalized(
    image: np.ndarray, roi: tuple[float, float, float, float]
) -> np.ndarray | None:
    height, width = image.shape[:2]
    x1 = max(0, int(roi[0] * width))
    y1 = max(0, int(roi[1] * height))
    x2 = min(width, int(roi[2] * width))
    y2 = min(height, int(roi[3] * height))
    if x2 - x1 < 24 or y2 - y1 < 24:
        return None
    return image[y1:y2, x1:x2]


def _crop_from_points(image: np.ndarray, points: np.ndarray) -> np.ndarray | None:
    pts = np.asarray(points, dtype=np.float32).reshape(-1, 2)
    if len(pts) < 4 or not np.isfinite(pts).all():
        return None
    height, width = image.shape[:2]
    min_x, min_y = pts.min(axis=0)
    max_x, max_y = pts.max(axis=0)
    box_w = max_x - min_x
    box_h = max_y - min_y
    if box_w < 12 or box_h < 12:
        return None
    pad_x = max(12.0, 0.25 * box_w)
    pad_y = max(12.0, 0.25 * box_h)
    x1 = max(0, int(min_x - pad_x))
    y1 = max(0, int(min_y - pad_y))
    x2 = min(width, int(max_x + pad_x))
    y2 = min(height, int(max_y + pad_y))
    if x2 - x1 < 24 or y2 - y1 < 24:
        return None
    return image[y1:y2, x1:x2]


def _preprocessing_variants(image: np.ndarray) -> Iterable[np.ndarray]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8)).apply(gray)
    yield clahe
    blur = cv2.GaussianBlur(clahe, (0, 0), 1.2)
    sharp = cv2.addWeighted(clahe, 1.6, blur, -0.6, 0)
    yield sharp
    block = max(15, min(51, (min(gray.shape[:2]) // 10) | 1))
    yield cv2.adaptiveThreshold(
        clahe,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        block,
        7,
    )


def _decode_frame(
    detector: cv2.QRCodeDetector,
    frame: np.ndarray,
    slot: str,
    today: date,
    *,
    try_curved: bool = True,
) -> tuple[QrParsedResult | None, bool, np.ndarray | None]:
    """Attempt single + multi + curved OpenCV decode on one frame."""
    saw_points = False
    first_points: np.ndarray | None = None
    fallback: QrParsedResult | None = None

    payload, points, _ = detector.detectAndDecode(frame)
    if points is not None:
        saw_points = True
        first_points = points
    if payload:
        parsed = parse_qr_payload(payload, slot, today=today)
        if parsed.status == "DECODED_VALID":
            return parsed, True, first_points
        fallback = parsed

    try:
        ok, decoded_list, multi_points, _ = detector.detectAndDecodeMulti(frame)
    except cv2.error:
        ok, decoded_list, multi_points = False, (), None
    if multi_points is not None:
        saw_points = True
        if first_points is None:
            first_points = multi_points[0] if len(multi_points) > 0 else None
    if ok and decoded_list:
        for item in decoded_list:
            if not item:
                continue
            parsed = parse_qr_payload(item, slot, today=today)
            if parsed.status == "DECODED_VALID":
                return parsed, True, first_points
            if fallback is None or (
                fallback.status == "UNSUPPORTED_FORMAT" and parsed.status == "INVALID_FORMAT"
            ):
                fallback = parsed

    if try_curved and saw_points:
        try:
            curved_payload, _, _ = detector.detectAndDecodeCurved(frame)
        except cv2.error:
            curved_payload = ""
        if curved_payload:
            parsed = parse_qr_payload(curved_payload, slot, today=today)
            if parsed.status == "DECODED_VALID":
                return parsed, True, first_points
            fallback = fallback or parsed

    return fallback, saw_points, first_points


def _views_for_orientation(image: np.ndarray) -> Iterable[np.ndarray]:
    if image.shape[0] > image.shape[1]:
        yield cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
        yield cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        yield image
    else:
        yield image
        yield cv2.rotate(image, cv2.ROTATE_180)


def _resize_max(image: np.ndarray, max_side: int) -> np.ndarray:
    height, width = image.shape[:2]
    scale = min(1.0, max_side / max(height, width))
    if scale == 1.0:
        return image
    return cv2.resize(
        image,
        (max(1, round(width * scale)), max(1, round(height * scale))),
        interpolation=cv2.INTER_AREA,
    )


def _infer_card_variant(slot: str, roi_kind: str | None) -> CardVariant:
    if roi_kind == "cccd_2021_front" or slot == "cccd_front":
        return "CCCD_2021"
    if roi_kind == "can_cuoc_2024_back" or slot == "cccd_back":
        return "CAN_CUOC_2024"
    if roi_kind == "gplx_pet_back" or slot.startswith("gplx"):
        return "GPLX_PET"
    return "UNKNOWN"


def decode_qr_image(
    image: np.ndarray,
    slot: str,
    *,
    warped: bool = False,
    try_fallback: bool = False,
    check: Callable[[], None] = lambda: None,
    today: date | None = None,
) -> QrDecodeResult:
    """Decode CCCD/Can Cuoc/GPLX QR via OpenCV across raw image, warped card, ROIs, and variants."""
    if not slot.startswith(("cccd", "gplx")):
        return QrDecodeResult(
            QrParsedResult(status="UNSUPPORTED_FORMAT", legacy_status="QR_FOUND_UNSUPPORTED"),
            document_side=slot_to_side(slot),
        )

    today = today or _default_today()
    try:
        detector = cv2.QRCodeDetector()
    except cv2.error:
        return QrDecodeResult(
            parsed=QrParsedResult(status="ENGINE_UNAVAILABLE", legacy_status="QR_DECODE_FAILED"),
            document_side=slot_to_side(slot),
        )
    saw_points = False
    saw_engine_error = False
    fallback: QrParsedResult | None = None
    doc_side = slot_to_side(slot)

    def attempt(frame: np.ndarray, roi_kind: str | None = None) -> QrDecodeResult | None:
        nonlocal saw_points, saw_engine_error, fallback
        check()
        try:
            parsed, pts_seen, pts = _decode_frame(detector, frame, slot, today)
        except cv2.error:
            saw_engine_error = True
            return None
        saw_points |= pts_seen
        if parsed is not None:
            if parsed.status == "DECODED_VALID":
                return QrDecodeResult(
                    parsed=parsed,
                    card_variant=_infer_card_variant(slot, roi_kind),
                    document_side=doc_side,
                )
            fallback = fallback or parsed

        if pts is not None:
            cropped = _crop_from_points(frame, pts)
            if cropped is not None:
                zoomed = _pad_quiet_zone(_upscale_min_side(cropped, 380), 24)
                for variant in (zoomed, *_preprocessing_variants(zoomed)):
                    check()
                    try:
                        v_parsed, v_pts, _ = _decode_frame(
                            detector, variant, slot, today, try_curved=False
                        )
                    except cv2.error:
                        continue
                    saw_points |= v_pts
                    if v_parsed is not None:
                        if v_parsed.status == "DECODED_VALID":
                            return QrDecodeResult(
                                parsed=v_parsed,
                                card_variant=_infer_card_variant(slot, roi_kind),
                                document_side=doc_side,
                            )
                        fallback = fallback or v_parsed
        return None

    if warped:
        # 1. Full warped landscape card + quiet zone (0 and 180 deg)
        for view in _views_for_orientation(image):
            if hit := attempt(_pad_quiet_zone(view, 24)):
                return hit

        # 2. Card-template QR ROIs prioritized by slot
        if slot.startswith("gplx"):
            roi_groups = (("gplx_pet_back", GPLX_BACK_ROIS),)
        elif slot == "cccd_front":
            roi_groups = (
                ("cccd_2021_front", CCCD_2021_FRONT_ROIS),
                ("can_cuoc_2024_back", CAN_CUOC_2024_BACK_ROIS),
            )
        else:
            roi_groups = (
                ("can_cuoc_2024_back", CAN_CUOC_2024_BACK_ROIS),
                ("cccd_2021_front", CCCD_2021_FRONT_ROIS),
            )
        top_crops: list[tuple[str, np.ndarray]] = []
        for kind, rois in roi_groups:
            for roi in rois:
                crop = _crop_normalized(image, roi)
                if crop is None:
                    continue
                prepared_roi = _pad_quiet_zone(_upscale_min_side(crop, 420), 24)
                top_crops.append((kind, prepared_roi))
                if hit := attempt(prepared_roi, roi_kind=kind):
                    return hit

        # 3. Bounded preprocessing variants on priority ROIs
        for kind, prepared_roi in top_crops[:2]:
            for variant in _preprocessing_variants(prepared_roi):
                if hit := attempt(variant, roi_kind=kind):
                    return hit
    else:
        base = _resize_max(image, 2500)
        for view in _views_for_orientation(base):
            if hit := attempt(view):
                return hit

        if try_fallback:
            # Multi-scale raw attempts without depending on YOLO/warp
            h, w = image.shape[:2]
            if max(h, w) >= 2200:
                if hit := attempt(_resize_max(image, 1400)):
                    return hit
            elif max(h, w) <= 1200:
                upscaled = cv2.resize(
                    base, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC
                )
                if hit := attempt(_pad_quiet_zone(upscaled, 24)):
                    return hit

            # Bounded quadrant ROIs + preprocessing variants
            for roi in RAW_QUADRANT_ROIS:
                crop = _crop_normalized(base, roi)
                if crop is None:
                    continue
                prepared_roi = _pad_quiet_zone(_upscale_min_side(crop, 420), 24)
                if hit := attempt(prepared_roi):
                    return hit
            for roi in RAW_QUADRANT_ROIS[:2]:
                crop = _crop_normalized(base, roi)
                if crop is None:
                    continue
                prepared_roi = _pad_quiet_zone(_upscale_min_side(crop, 420), 24)
                for variant in _preprocessing_variants(prepared_roi):
                    if hit := attempt(variant):
                        return hit

    if fallback is not None:
        return QrDecodeResult(parsed=fallback, document_side=doc_side)
    if saw_engine_error and not saw_points:
        return QrDecodeResult(
            parsed=QrParsedResult(status="ENGINE_UNAVAILABLE", legacy_status="QR_DECODE_FAILED"),
            document_side=doc_side,
        )
    status: QrStatus = "DETECTED_UNREADABLE" if saw_points else "NOT_FOUND"
    legacy = "QR_DECODE_FAILED" if saw_points else "QR_NOT_FOUND"
    return QrDecodeResult(
        parsed=QrParsedResult(status=status, legacy_status=legacy),
        document_side=doc_side,
    )


def normalized_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).lower()


def fold_diacritics(value: str) -> str:
    decomposed = unicodedata.normalize("NFD", value)
    return (
        "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
        .replace("đ", "d")
        .replace("Đ", "D")
    )


def compare_name_values(left: str | None, right: str | None) -> str | None:
    if not left or not right:
        return None
    norm_left = normalized_name(left)
    norm_right = normalized_name(right)
    if not norm_left or not norm_right:
        return None
    if norm_left == norm_right:
        return "EXACT_MATCH"
    if fold_diacritics(norm_left) == fold_diacritics(norm_right):
        return "DIACRITIC_MISMATCH"
    return "NAME_MISMATCH"


def normalize_iso_date(value: Any) -> str | None:
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned).isoformat()
    except ValueError:
        parsed = parse_qr_date(cleaned)
        return parsed.isoformat() if parsed else None


@dataclass(frozen=True)
class CrossValidationSummary:
    status: CrossValidationStatus
    conflict_fields: tuple[str, ...]
    diacritic_mismatch_fields: tuple[str, ...]
    matched_fields: tuple[str, ...]


def _normalized_class_set(value: Any) -> set[str] | None:
    if isinstance(value, (list, tuple, set)):
        tokens = [str(item).strip().upper() for item in value if str(item).strip()]
    elif isinstance(value, str):
        tokens = [tok.strip().upper() for tok in re.split(r"[,;/\s]+", value) if tok.strip()]
    else:
        return None
    return set(tokens) if tokens else None


def compare_qr_and_ocr_fields(
    qr_fields: Mapping[str, Any],
    ocr_fields: Mapping[str, Any],
) -> CrossValidationSummary:
    """Compare normalized QR and OCR fields per Phase 11.1.3A and GPLX QR rules."""
    conflicts: list[str] = []
    diacritic_mismatches: list[str] = []
    matched: list[str] = []

    if (q_id := qr_fields.get("id_number")) and (o_id := ocr_fields.get("id_number")):
        if str(q_id).strip().upper() == str(o_id).strip().upper():
            matched.append("id_number")
        else:
            conflicts.append("id_number")

    if (q_dob := normalize_iso_date(qr_fields.get("dob"))) and (
        o_dob := normalize_iso_date(ocr_fields.get("dob"))
    ):
        if q_dob == o_dob:
            matched.append("dob")
        else:
            conflicts.append("dob")

    if (q_name := qr_fields.get("full_name")) and (o_name := ocr_fields.get("full_name")):
        cmp = compare_name_values(str(q_name), str(o_name))
        if cmp == "EXACT_MATCH":
            matched.append("full_name")
        elif cmp == "DIACRITIC_MISMATCH":
            conflicts.append("full_name")
            diacritic_mismatches.append("full_name")
        elif cmp == "NAME_MISMATCH":
            conflicts.append("full_name")

    if (q_classes := _normalized_class_set(qr_fields.get("vehicle_class"))) and (
        o_classes := _normalized_class_set(ocr_fields.get("vehicle_class"))
    ):
        if q_classes == o_classes:
            matched.append("vehicle_class")
        else:
            conflicts.append("vehicle_class")

    if (q_gender := qr_fields.get("gender")) and (o_gender := ocr_fields.get("gender")):
        if str(q_gender).strip().upper() == str(o_gender).strip().upper():
            matched.append("gender")
        else:
            conflicts.append("gender")

    if (q_issue := normalize_iso_date(qr_fields.get("issue_date"))) and (
        o_issue := normalize_iso_date(ocr_fields.get("issue_date"))
    ):
        if q_issue == o_issue:
            matched.append("issue_date")
        else:
            conflicts.append("issue_date")

    if conflicts:
        status: CrossValidationStatus = "CONFLICT"
    elif any(field in matched for field in ("id_number", "full_name", "dob", "vehicle_class")):
        status = "MATCHED"
    else:
        status = "INSUFFICIENT_EVIDENCE"

    return CrossValidationSummary(
        status=status,
        conflict_fields=tuple(conflicts),
        diacritic_mismatch_fields=tuple(diacritic_mismatches),
        matched_fields=tuple(matched),
    )


def compute_lookup_hmac(value: str, secret_key: bytes) -> str:
    """Compute a keyed HMAC-SHA256 hex digest for privacy-preserving duplicate lookups."""
    normalized = value.strip()
    return hmac.new(secret_key, normalized.encode("utf-8"), hashlib.sha256).hexdigest()
