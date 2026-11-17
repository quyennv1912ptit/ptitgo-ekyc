"""Build the same warnings for the driver, review, and activation policy."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from ekyc.services.driver_domain import name_comparison

TEXT = {
    "IMAGE_BLURRY": "Ảnh có dấu hiệu mờ.",
    "IMAGE_GLARE": "Ảnh có vùng lóa.",
    "IMAGE_TOO_SMALL": "Ảnh có độ phân giải thấp.",
    "CARD_CROPPED": "Có thể ảnh đã cắt mất một phần thẻ.",
    "CARD_NOT_FOUND": "Chưa xác định được vùng thẻ.",
    "OCR_INCOMPLETE": "Lượt đọc chữ chưa lấy đủ thông tin.",
    "QR_OCR_CONFLICT": "Thông tin trích xuất từ các nguồn chưa khớp.",
    "NAME_DIACRITIC_MISMATCH": "Họ tên giữa hai giấy tờ khác dấu.",
    "NAME_MISMATCH": "Họ tên giữa hai giấy tờ khác nhau.",
    "DOB_MISMATCH": "Ngày sinh giữa hai giấy tờ khác nhau.",
    "CCCD_EXPIRED": "Ngày hết hạn căn cước đã qua.",
    "CCCD_EXPIRY_UNKNOWN": "Chưa xác định được thời hạn căn cước.",
    "GPLX_EXPIRED": "Ngày hết hạn GPLX đã qua.",
    "GPLX_EXPIRY_UNKNOWN": "Chưa xác định được thời hạn GPLX.",
    "GPLX_CLASS_UNKNOWN": "Có hạng GPLX chưa xác định.",
    "GPLX_RESTRICTION_UNKNOWN": "Chưa xác định được hạn chế trên GPLX.",
    "FACE_NOT_EVALUATED": "Chưa thực hiện đối chiếu khuôn mặt.",
    "FACE_INSUFFICIENT_QUALITY": "Ảnh chưa đủ chất lượng để đối chiếu khuôn mặt.",
    "FACE_POSSIBLE_MISMATCH": "Có dấu hiệu không tương đồng giữa các ảnh chân dung.",
    "DOCUMENT_REQUIRED": "Còn thiếu ảnh bắt buộc.",
}
FACE_QUALITY_MESSAGES = {
    "NO_FACE": "Chưa tìm thấy khuôn mặt trong ảnh này.",
    "MULTIPLE_FACES": "Ảnh có nhiều khuôn mặt.",
    "FACE_TOO_SMALL": "Vùng khuôn mặt quá nhỏ.",
    "FACE_CROPPED": "Vùng khuôn mặt bị cắt.",
    "FACE_LANDMARKS_UNRELIABLE": "Chưa xác định được các điểm trên khuôn mặt.",
    "FACE_ALIGNMENT_FAILED": "Chưa căn chỉnh được khuôn mặt.",
    "FACE_BLURRY": "Vùng khuôn mặt bị mờ.",
    "FACE_GLARE": "Vùng khuôn mặt bị lóa.",
    "FACE_TOO_DARK": "Vùng khuôn mặt quá tối.",
    "CARD_PREPARATION_FAILED": "Chưa tìm được vùng ảnh chân dung trên thẻ.",
    "INVALID_IMAGE": "Chưa đọc được ảnh để kiểm tra khuôn mặt.",
}
FACE_WARNING_SLOTS = {"cccd_front", "gplx_front", "selfie"}

IMAGE_CODES = {"IMAGE_BLURRY", "IMAGE_GLARE", "IMAGE_TOO_SMALL", "CARD_CROPPED", "CARD_NOT_FOUND"}
REQUIRED_SLOTS = ("cccd_front", "cccd_back", "gplx_front", "selfie")
FIELD_NAMES = {
    "CCCD": {
        "id_number": "cccd_id",
        "full_name": "cccd_name",
        "dob": "cccd_dob",
        "gender": "cccd_gender",
        "expiry": "cccd_expiry_date",
    },
    "GPLX": {
        "id_number": "gplx_number",
        "full_name": "gplx_name",
        "dob": "gplx_dob",
        "vehicle_class": "gplx_classes",
        "expiry": "expiry_date",
    },
}


def local_today():
    return datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date()


def as_date(value):
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def document_type(slot):
    return slot.split("_", 1)[0].upper()


def verification_warnings(
    payload, revision, license_classes, *, today=None, face_statuses=(), face_quality=None
):
    today = local_today() if today is None else today
    warnings = {}

    def add(
        code,
        source,
        *,
        slot=None,
        doc=None,
        field=None,
        severity="WARNING",
        block=False,
        message=None,
    ):
        doc = doc or (document_type(slot) if slot else None)
        action = {
            "CAPTURE": "Chụp lại ảnh theo khung hướng dẫn.",
            "DETECTOR": "Chụp rõ toàn bộ thẻ và đủ bốn góc.",
            "OCR": "Đối chiếu thông tin với giấy tờ gốc.",
            "QR_OCR": "Đối chiếu hai đề xuất với giấy tờ gốc.",
            "CONFIRMED_FIELDS": "Kiểm tra và sửa thông tin theo giấy tờ gốc.",
            "FACE_COMPARISON": "Xem lại ảnh chân dung và selfie.",
        }.get(source, "Đối chiếu hồ sơ trước khi xét duyệt.")
        if code.endswith("_EXPIRED") and block:
            action = "Bổ sung giấy tờ còn hiệu lực."
        warnings[code, slot, doc, field] = {
            "code": code,
            "severity": severity,
            "documentType": doc,
            "slot": slot,
            "fieldKey": field,
            "humanReadableMessage": message or TEXT[code],
            "recommendedAction": action,
            "evidenceSource": source,
            "documentRevision": revision,
            "requiresReview": severity != "INFO",
            "blocksSubmission": severity == "BLOCKING",
            "blocksActivation": block,
        }

    captures = payload.get("captures") or {}
    fields = payload.get("confirmed") or {}
    for slot in REQUIRED_SLOTS:
        if slot not in captures:
            add("DOCUMENT_REQUIRED", "CAPTURE", slot=slot, severity="BLOCKING")
    for slot, capture in captures.items():
        for code in (capture.get("quality") or {}).get("warnings", []):
            code = "IMAGE_TOO_SMALL" if code == "SMALL" else code
            if code in IMAGE_CODES:
                add(code, "CAPTURE", slot=slot)
    cccd_ocr_expiry = None
    cccd_ocr_no_expiry = False
    cccd_ocr_attempted = False
    for slot, proposal in (payload.get("proposals") or {}).items():
        if not proposal.get("captureId") or proposal["captureId"] != (captures.get(slot) or {}).get(
            "id"
        ):
            continue
        result = proposal.get("result") or {}
        for code in result.get("warnings", []):
            code = {
                "DETECTOR_NO_BOX": "CARD_NOT_FOUND",
                "DETECTOR_CARD_NOT_FOUND": "CARD_NOT_FOUND",
            }.get(code, code)
            if code in IMAGE_CODES:
                add(code, "DETECTOR", slot=slot)
        required = {
            "cccd_front": {"id_number", "full_name", "dob"},
            "gplx_front": {"id_number", "full_name", "dob", "vehicle_class", "expiry"},
        }.get(slot, set())
        valid = {
            candidate.get("fieldKey")
            for candidate in result.get("candidates", [])
            if candidate.get("validationStatus") == "valid" and not candidate.get("conflict")
        }
        status = result.get("ocrStatus")
        if status in {"OCR_FAILED", "OCR_NO_FIELDS", "OCR_NO_TEXT"} or (
            status == "OCR_READ" and not required <= valid
        ):
            add("OCR_INCOMPLETE", "OCR", slot=slot)
        doc = document_type(slot)
        for key in result.get("conflictFields", []):
            sources = {
                candidate["source"]
                for candidate in result.get("candidates", [])
                if candidate.get("fieldKey") == key
                and candidate.get("conflict")
                and candidate.get("source")
            }
            if {"QR", "OCR"} <= sources:
                add("QR_OCR_CONFLICT", "QR_OCR", slot=slot, field=FIELD_NAMES.get(doc, {}).get(key))
            elif "OCR" in sources:
                add("OCR_INCOMPLETE", "OCR", slot=slot, field=FIELD_NAMES.get(doc, {}).get(key))
        statuses = result.get("fieldStatuses") or {}
        if slot == "gplx_front":
            if statuses.get("gplx_expiry") in {
                "OCR_MISSING",
                "PARSE_FAILED",
                "VALIDATION_FAILED",
            } and not (fields.get("no_expiry") is True or as_date(fields.get("expiry_date"))):
                add("GPLX_EXPIRY_UNKNOWN", "OCR", slot=slot, field="expiry_date")
            if statuses.get("gplx_restriction") in {"UNKNOWN", "UNREADABLE"}:
                add("GPLX_RESTRICTION_UNKNOWN", "OCR", slot=slot, severity="CRITICAL", block=True)
        if slot == "cccd_front":
            cccd_ocr_attempted = result.get("ocrStatus") in {
                "OCR_READ",
                "OCR_NO_FIELDS",
                "OCR_NO_TEXT",
                "OCR_FAILED",
            }
            if "ORIENTATION_UNCERTAIN" not in result.get("warnings", []):
                expiry = next(
                    (
                        candidate.get("candidateValue")
                        for candidate in result.get("candidates", [])
                        if candidate.get("fieldKey") == "expiry"
                        and candidate.get("source") == "OCR"
                        and candidate.get("parseStatus") == "parsed"
                        and candidate.get("validationStatus") == "valid"
                        and not candidate.get("conflict")
                    ),
                    None,
                )
                if isinstance(expiry, dict):
                    if expiry.get("no_expiry") is False:
                        cccd_ocr_expiry = as_date(expiry.get("expiry_date"))
                    else:
                        cccd_ocr_no_expiry = (
                            expiry.get("no_expiry") is True
                            and "expiry_date" in expiry
                            and expiry["expiry_date"] is None
                        )
    name = name_comparison(fields.get("cccd_name"), fields.get("gplx_name"))
    if name in {"DIACRITIC_MISMATCH", "NAME_MISMATCH"}:
        add(
            "NAME_DIACRITIC_MISMATCH" if name == "DIACRITIC_MISMATCH" else name,
            "CONFIRMED_FIELDS",
            doc="GPLX",
            field="gplx_name",
        )
    left, right = fields.get("cccd_dob"), fields.get("gplx_dob")
    if left and right and left != right:
        add("DOB_MISMATCH", "CONFIRMED_FIELDS", doc="GPLX", field="gplx_dob", severity="CRITICAL")
    cccd_date = as_date(fields.get("cccd_expiry_date"))
    if cccd_date is not None and cccd_date < today:
        add(
            "CCCD_EXPIRED",
            "CONFIRMED_FIELDS",
            slot="cccd_front",
            field="cccd_expiry_date",
            severity="CRITICAL",
            block=True,
        )
    elif cccd_date is None and cccd_ocr_attempted:
        if cccd_ocr_expiry is not None and cccd_ocr_expiry < today:
            add(
                "CCCD_EXPIRED",
                "OCR",
                slot="cccd_front",
                field="cccd_expiry_date",
                severity="CRITICAL",
                block=True,
            )
        elif cccd_ocr_expiry is None and not cccd_ocr_no_expiry:
            add(
                "CCCD_EXPIRY_UNKNOWN",
                "OCR",
                slot="cccd_front",
                field="cccd_expiry_date",
                severity="INFO",
            )
    if fields:
        expiry = as_date(fields.get("expiry_date"))
        if expiry is not None and expiry < today:
            add(
                "GPLX_EXPIRED",
                "CONFIRMED_FIELDS",
                slot="gplx_front",
                field="expiry_date",
                severity="CRITICAL",
                block=True,
            )
        elif expiry is None and fields.get("no_expiry") is not True:
            add(
                "GPLX_EXPIRY_UNKNOWN",
                "CONFIRMED_FIELDS",
                slot="gplx_front",
                field="expiry_date",
                severity="CRITICAL",
                block=True,
            )
        classes = fields.get("gplx_classes") or []
        if not classes or any(code not in license_classes for code in classes):
            add(
                "GPLX_CLASS_UNKNOWN",
                "CONFIRMED_FIELDS",
                slot="gplx_front",
                field="gplx_classes",
                severity="CRITICAL",
                block=True,
            )
    if "POSSIBLE_MISMATCH" in face_statuses:
        add("FACE_POSSIBLE_MISMATCH", "FACE_COMPARISON", slot="selfie", severity="CRITICAL")
    flagged_slots = sorted(
        slot
        for slot, code in (face_quality or {}).items()
        if slot in FACE_WARNING_SLOTS and code in FACE_QUALITY_MESSAGES
    )
    for slot in flagged_slots:
        add(
            "FACE_INSUFFICIENT_QUALITY",
            "FACE_COMPARISON",
            slot=slot,
            message=FACE_QUALITY_MESSAGES[face_quality[slot]],
        )
    if "INSUFFICIENT_QUALITY" in face_statuses and not flagged_slots:
        add("FACE_INSUFFICIENT_QUALITY", "FACE_COMPARISON")
    if not face_statuses or set(face_statuses) == {"NOT_EVALUATED"}:
        add("FACE_NOT_EVALUATED", "FACE_COMPARISON", slot="selfie", severity="INFO")
    return list(warnings.values())
