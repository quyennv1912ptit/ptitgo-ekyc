from datetime import date

from ekyc.services.verification_warnings import REQUIRED_SLOTS, verification_warnings


def test_revision_and_sources_follow_capture_and_confirmed_fields():
    captures = {slot: {"id": slot} for slot in REQUIRED_SLOTS}
    result = {
        "warnings": ["DETECTOR_NO_BOX"],
        "ocrStatus": "OCR_NO_FIELDS",
        "conflictFields": ["full_name"],
        "candidates": [
            {"source": source, "fieldKey": "full_name", "conflict": True}
            for source in ("QR", "OCR")
        ],
    }
    payload = {
        "captures": captures,
        "proposals": {"cccd_front": {"captureId": "cccd_front", "result": result}},
        "confirmed": {
            "cccd_name": "ĐỖ MẪU",
            "gplx_name": "DO MAU",
            "cccd_dob": "2000-01-01",
            "gplx_dob": "2000-01-01",
            "no_expiry": False,
            "expiry_date": "2026-11-16",
            "gplx_classes": ["B"],
        },
    }
    today = date(2026, 11, 16)
    first = verification_warnings(payload, 4, {"B"}, today=today)
    assert {item["code"] for item in first} == {
        "CARD_NOT_FOUND",
        "OCR_INCOMPLETE",
        "QR_OCR_CONFLICT",
        "CCCD_EXPIRY_UNKNOWN",
        "NAME_DIACRITIC_MISMATCH",
        "FACE_NOT_EVALUATED",
    }
    assert all(item["documentRevision"] == 4 for item in first)
    payload["captures"]["cccd_front"] = {"id": "new"}
    payload["confirmed"].update(gplx_name="A MẪU", gplx_dob="2000-01-02")
    second = verification_warnings(payload, 5, {"B"}, today=date(2026, 11, 17))
    assert {item["code"] for item in second} == {
        "GPLX_EXPIRED",
        "NAME_MISMATCH",
        "DOB_MISMATCH",
        "FACE_NOT_EVALUATED",
    }
    assert next(item for item in second if item["blocksActivation"])["code"] == "GPLX_EXPIRED"


def test_missing_license_expiry_is_unknown_and_face_advice_is_not_an_activation_gate():
    warnings = verification_warnings(
        {"confirmed": {"no_expiry": False, "gplx_classes": ["B"]}},
        2,
        {"B"},
        face_statuses=["POSSIBLE_MISMATCH", "INSUFFICIENT_QUALITY"],
    )
    by_code = {item["code"]: item for item in warnings}
    assert "GPLX_EXPIRED" not in by_code
    assert by_code["GPLX_EXPIRY_UNKNOWN"]["blocksActivation"]
    assert not by_code["FACE_POSSIBLE_MISMATCH"]["blocksActivation"]
    assert "FACE_NOT_EVALUATED" not in by_code


def test_cccd_expiry_uses_structured_ocr_value_with_orientation_evidence():
    candidate = {
        "fieldKey": "expiry",
        "source": "OCR",
        "parseStatus": "parsed",
        "validationStatus": "valid",
        "candidateValue": {"no_expiry": False, "expiry_date": "2020-01-01"},
    }
    result = {"ocrStatus": "OCR_READ", "candidates": [candidate], "warnings": []}
    payload = {
        "captures": {"cccd_front": {"id": "current"}},
        "proposals": {"cccd_front": {"captureId": "current", "result": result}},
    }

    def codes():
        return {
            item["code"]
            for item in verification_warnings(payload, 6, {"B"}, today=date(2026, 11, 16))
        }

    assert "CCCD_EXPIRED" in codes()
    result["warnings"] = ["ORIENTATION_UNCERTAIN"]
    assert "CCCD_EXPIRED" not in codes()
    assert "CCCD_EXPIRY_UNKNOWN" in codes()
    result["warnings"] = []
    candidate["candidateValue"] = {"no_expiry": True, "expiry_date": None}
    assert not {"CCCD_EXPIRED", "CCCD_EXPIRY_UNKNOWN"} & codes()
