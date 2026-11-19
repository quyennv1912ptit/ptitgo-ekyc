import io
import logging
from datetime import date
from types import SimpleNamespace

import cv2
import numpy as np
from PIL import Image

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.card_image import PreparedCard
from ekyc.pipeline.doc_types import DocClassification, DocType, Side
from ekyc.pipeline.qr import (
    compare_qr_and_ocr_fields,
    compute_lookup_hmac,
    decode_qr_image,
    parse_cccd_qr_payload,
)
from ekyc.services.document_extraction import DocumentExtractor
from ekyc.services.verification_warnings import REQUIRED_SLOTS, verification_warnings

TODAY = date(2026, 11, 19)


def _encode_png(bgr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)).save(buf, format="PNG")
    return buf.getvalue()


def _make_qr_card(payload: str, *, width=1600, height=1009, region="front_tr", blur=False):
    qr = cv2.QRCodeEncoder_create().encode(payload)
    qr = cv2.copyMakeBorder(qr, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    qr_bgr = cv2.cvtColor(
        cv2.resize(qr, (220, 220), interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2BGR
    )
    if blur:
        qr_bgr = cv2.GaussianBlur(qr_bgr, (5, 5), 1.1)
    card = np.full((height, width, 3), 230, dtype=np.uint8)
    if region == "front_tr":
        y0, x0 = 50, width - 260
    elif region == "back_mr":
        y0, x0 = 260, width - 380
    else:
        y0, x0 = 60, 60
    card[y0 : y0 + 220, x0 : x0 + 220] = qr_bgr
    return card


def test_parse_cccd_2021_and_can_cuoc_2024_variants():
    # CCCD 2021 7-field standard
    p2021 = parse_cccd_qr_payload(
        "046096003093|191234567|NGUYỄN VĂN MẪU|01011996|Nam|Số 1 Đường Giải Phóng, Hà Nội|01052021",
        "cccd_front",
        today=TODAY,
    )
    assert p2021.status == "DECODED_VALID"
    assert p2021.fields == {
        "id_number": "046096003093",
        "full_name": "NGUYỄN VĂN MẪU",
        "dob": "1996-01-01",
        "gender": "MALE",
        "address": "Số 1 Đường Giải Phóng, Hà Nội",
        "issue_date": "2021-05-01",
    }
    assert "expiry" not in p2021.fields
    assert "old_cmnd" not in p2021.fields

    # Can Cuoc 2024 9-field layout with slashes and under-14 age (no 14-year gap rule!)
    p2024_child = parse_cccd_qr_payload(
        "046222000123||TRẦN BÉ MẪU|01/05/2022|Nữ|Phường Bến Nghé, TP.HCM|"
        "15/07/2024|046190000001|TRẦN VĂN CHA",
        "cccd_back",
        today=TODAY,
    )
    assert p2024_child.status == "DECODED_VALID"
    assert p2024_child.fields["dob"] == "2022-05-01"
    assert p2024_child.fields["issue_date"] == "2024-07-15"
    assert p2024_child.fields["gender"] == "FEMALE"
    assert set(p2024_child.fields) <= {
        "id_number",
        "full_name",
        "dob",
        "gender",
        "address",
        "issue_date",
    }

    # 6-field layout without old CMND slot
    p6 = parse_cccd_qr_payload(
        "046096003094|LÊ THỊ MẪU|02031995|Nữ|Đà Nẵng|10082024",
        "cccd_back",
        today=TODAY,
    )
    assert p6.status == "DECODED_VALID"
    assert p6.fields["full_name"] == "LÊ THỊ MẪU"
    assert p6.fields["issue_date"] == "2024-08-10"


def test_parse_missing_fields_and_invalid_vs_unsupported_formats():
    partial = parse_cccd_qr_payload(
        "046096003093||NGUYỄN VĂN MẪU|01011996|||",
        "cccd_front",
        today=TODAY,
    )
    assert partial.status == "DECODED_VALID"
    assert set(partial.missing_fields) == {"gender", "address", "issue_date"}
    assert "QR_PARTIAL_FIELDS" in partial.warnings

    # Both name and dob missing -> invalid
    too_empty = parse_cccd_qr_payload("046096003093||||Nam||", "cccd_front", today=TODAY)
    assert too_empty.status == "INVALID_FORMAT"

    # Bad ID length / invalid calendar date / issue before dob
    assert (
        parse_cccd_qr_payload(
            "04609600309||NGUYỄN VĂN MẪU|01011996|Nam|Hà Nội|01052021",
            "cccd_front",
            today=TODAY,
        ).status
        == "INVALID_FORMAT"
    )
    assert (
        parse_cccd_qr_payload(
            "046096003093||NGUYỄN VĂN MẪU|31021996|Nam|Hà Nội|01052021",
            "cccd_front",
            today=TODAY,
        ).status
        == "INVALID_FORMAT"
    )
    assert (
        parse_cccd_qr_payload(
            "046096003093||NGUYỄN VĂN MẪU|01052020|Nam|Hà Nội|01052019",
            "cccd_front",
            today=TODAY,
        ).status
        == "INVALID_FORMAT"
    )

    # Non-CCCD URL or GPLX slot -> UNSUPPORTED_FORMAT
    assert (
        parse_cccd_qr_payload("https://example.invalid/qr", "cccd_front", today=TODAY).status
        == "UNSUPPORTED_FORMAT"
    )
    assert (
        parse_cccd_qr_payload(
            "046096003093||NGUYỄN VĂN MẪU|01011996|Nam|Hà Nội|01052021",
            "gplx_front",
            today=TODAY,
        ).status
        == "UNSUPPORTED_FORMAT"
    )


def test_decode_tilted_cccd2021_and_can_cuoc_2024_via_warped_roi_and_raw_fallback(settings):
    payload_2021 = "046096003093||NGUYỄN VĂN MẪU|01011996|Nam|Hà Nội|01052021"
    card_2021 = _make_qr_card(payload_2021, region="front_tr", blur=True)
    # Tilt onto noisy background so raw pass fails, warped ROI recovers it
    bg = np.full((1500, 2000, 3), 120, dtype=np.uint8)
    src = np.float32([[0, 0], [1600, 0], [1600, 1009], [0, 1009]])
    dst = np.float32([[220, 180], [1720, 310], [1640, 1270], [160, 1140]])
    cv2.warpPerspective(
        card_2021,
        M := cv2.getPerspectiveTransform(src, dst),
        (2000, 1500),
        dst=bg,
        borderMode=cv2.BORDER_TRANSPARENT,
    )
    assert M is not None

    prep = SimpleNamespace(prepare_loaded=lambda loaded, diagnostics=True: PreparedCard(card_2021))
    ocr = SimpleNamespace(read_lines=lambda _: [])
    cls = SimpleNamespace(
        classify=lambda *_: DocClassification(DocType.CCCD, Side.FRONT, 0.9, 0, [], True)
    )
    result = DocumentExtractor(settings, prep, ocr, cls).extract(_encode_png(bg), "cccd_front")
    assert result.qr is not None
    assert result.qr.status == "DECODED_VALID"
    assert result.qr.card_variant == "CCCD_2021"
    assert result.qr.document_side == "FRONT"
    assert result.qr.bound_to_capture is True
    assert result.qr.cross_validation_status == "INSUFFICIENT_EVIDENCE"
    assert result.qr.fields["id_number"] == "046096003093"

    # Can Cuoc 2024 on cccd_back even when YOLO/warp fails (RAW_FRAME_FALLBACK)
    payload_2024 = "046096003095||TRẦN THỊ MẪU|12/04/1998|Nữ|TP.HCM|15/08/2024"
    card_2024_back = _make_qr_card(payload_2024, region="back_mr", blur=False)
    # Use a low-res crop where raw quadrant fallback finds it even when prepare fails
    small_back = cv2.resize(card_2024_back, (900, 567), interpolation=cv2.INTER_AREA)

    def fail_prepare(_loaded, diagnostics=True):
        raise EkycError(ErrorCode.CARD_NOT_FOUND)

    cls_back = SimpleNamespace(
        classify=lambda *_: DocClassification(DocType.CCCD, Side.BACK, 0.9, 0, [], True)
    )
    back_res = DocumentExtractor(
        settings, SimpleNamespace(prepare_loaded=fail_prepare), ocr, cls_back
    ).extract(_encode_png(small_back), "cccd_back")
    assert back_res.qr is not None
    assert back_res.qr.status == "DECODED_VALID"
    assert back_res.qr.document_side == "BACK"
    assert back_res.qr.fields["issue_date"] == "2024-08-15"

    # Old CCCD without QR -> NOT_FOUND
    blank = np.full((600, 950, 3), 235, dtype=np.uint8)
    no_qr = decode_qr_image(blank, "cccd_front", warped=True)
    assert no_qr.parsed.status == "NOT_FOUND"


def test_detected_unreadable_and_engine_unavailable(settings, monkeypatch):
    # Finder patterns present, data area corrupted -> DETECTED_UNREADABLE
    qr = cv2.QRCodeEncoder_create().encode(
        "046096003093||NGUYỄN VĂN MẪU|01011996|Nam|Hà Nội|01052021"
    )
    qr = cv2.copyMakeBorder(qr, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
    img = cv2.cvtColor(
        cv2.resize(qr, (320, 320), interpolation=cv2.INTER_NEAREST), cv2.COLOR_GRAY2BGR
    )
    # Wipe middle/bottom payload modules while keeping all 3 finder patterns intact
    img[110:220, 110:220] = 255
    img[20:100, 120:220] = 255
    res = decode_qr_image(img, "cccd_front")
    assert res.parsed.status in {"DETECTED_UNREADABLE", "NOT_FOUND"}

    # Explicitly test DETECTED_UNREADABLE and ENGINE_UNAVAILABLE via QRCodeDetector mock
    class UnreadableDet:
        def detectAndDecode(self, frame):
            return "", np.float32([[[10, 10], [80, 10], [80, 80], [10, 80]]]), None

        def detectAndDecodeMulti(self, frame):
            return False, (), None, None

        def detectAndDecodeCurved(self, frame):
            return "", None, None

    monkeypatch.setattr(cv2, "QRCodeDetector", UnreadableDet)
    assert decode_qr_image(img, "cccd_front").parsed.status == "DETECTED_UNREADABLE"

    class BrokenDet:
        def detectAndDecode(self, frame):
            raise cv2.error("synthetic opencv failure")

    monkeypatch.setattr(cv2, "QRCodeDetector", BrokenDet)
    extractor = DocumentExtractor(
        settings,
        SimpleNamespace(prepare_loaded=lambda loaded, diagnostics=True: PreparedCard(loaded.bgr)),
        SimpleNamespace(read_lines=lambda _: []),
    )
    out = extractor.extract(_encode_png(img), "cccd_front", ocr=False)
    assert out.qr is not None
    assert out.qr.status == "ENGINE_UNAVAILABLE"


def test_cross_validation_rules_and_cross_slot_warnings(settings):
    # No fuzzy ID match: 1-digit difference must conflict
    diff_id = compare_qr_and_ocr_fields(
        {"id_number": "046096003093", "dob": "1996-01-01", "full_name": "NGUYỄN VĂN MẪU"},
        {"id_number": "046096003094", "dob": "01/01/1996", "full_name": "NGUYEN VAN MAU"},
    )
    assert diff_id.status == "CONFLICT"
    assert set(diff_id.conflict_fields) == {"id_number", "full_name"}
    assert diff_id.diacritic_mismatch_fields == ("full_name",)
    assert diff_id.matched_fields == ("dob",)

    # Cross-slot: Can Cuoc 2024 QR on cccd_back vs OCR on cccd_front
    captures = {slot: {"id": f"cap-{slot}"} for slot in REQUIRED_SLOTS}
    payload = {
        "captures": captures,
        "proposals": {
            "cccd_front": {
                "captureId": "cap-cccd_front",
                "result": {
                    "ocrStatus": "OCR_READ",
                    "candidates": [
                        {
                            "fieldKey": "id_number",
                            "candidateValue": "046096003099",
                            "source": "OCR",
                            "parseStatus": "parsed",
                            "validationStatus": "valid",
                        },
                        {
                            "fieldKey": "full_name",
                            "candidateValue": "NGUYEN VAN MAU",
                            "source": "OCR",
                            "parseStatus": "parsed",
                            "validationStatus": "valid",
                        },
                        {
                            "fieldKey": "dob",
                            "candidateValue": "1996-01-01",
                            "source": "OCR",
                            "parseStatus": "parsed",
                            "validationStatus": "valid",
                        },
                    ],
                },
            },
            "cccd_back": {
                "captureId": "cap-cccd_back",
                "result": {
                    "ocrStatus": "OCR_READ",
                    "qr": {
                        "status": "DECODED_VALID",
                        "source": "QR_IMAGE",
                        "fields": {
                            "id_number": "046096003093",
                            "full_name": "NGUYỄN VĂN MẪU",
                            "dob": "1996-01-01",
                        },
                    },
                    "candidates": [],
                },
            },
        },
    }
    warnings = verification_warnings(payload, 3, {"A1"})
    codes = {(w["code"], w.get("fieldKey")) for w in warnings}
    assert ("QR_OCR_CONFLICT", "cccd_id") in codes
    assert ("QR_OCR_NAME_DIACRITIC_MISMATCH", "cccd_name") in codes


def test_lookup_hmac_privacy_and_api_qr_scan(settings, tmp_path, monkeypatch, caplog):
    from fastapi.testclient import TestClient
    from firebase_admin import auth
    from pydantic import SecretStr

    from ekyc.main import create_app

    digest1 = compute_lookup_hmac("046096003093", b"secret-server-key")
    digest2 = compute_lookup_hmac("046096003093", b"other-server-key")
    assert digest1 != digest2
    assert "046096003093" not in digest1 and len(digest1) == 64

    project = "synthetic-project"

    def verify(token, *, app, check_revoked):
        return {
            "sub": token,
            "aud": project,
            "iss": f"https://securetoken.google.com/{project}",
            "driver_verification_admin": token == "admin",
        }

    monkeypatch.setattr(auth, "verify_id_token", verify)
    app = create_app(
        settings.model_copy(
            update={
                "firebase_project_id": project,
                "verification_db_path": tmp_path / "qr_test.sqlite3",
                "verification_lookup_hmac_key": SecretStr("unit-test-hmac-secret-32-chars!!"),
            }
        )
    )
    caplog.set_level(logging.INFO)
    raw_secret_payload = "046096003093||NGUYỄN VĂN MẪU|01011996|Nam|Địa chỉ riêng|01052021"
    with TestClient(app) as client:
        driver_a = {"Authorization": "Bearer driver-a"}
        driver_b = {"Authorization": "Bearer driver-b"}
        app_a = client.post(
            "/ekyc/driver-verification/v1/applications", headers=driver_a
        ).json()["data"]
        path_a = f"/ekyc/driver-verification/v1/applications/{app_a['id']}"

        # Unauthorized & wrong-owner access
        assert (
            client.post(
                f"{path_a}/qr-scan", json={"revision": 0, "rawPayload": raw_secret_payload}
            ).status_code
            == 401
        )
        assert (
            client.post(
                f"{path_a}/qr-scan",
                headers=driver_b,
                json={"revision": 0, "rawPayload": raw_secret_payload},
            ).status_code
            == 404
        )

        # Reject frontend-supplied pre-parsed fields
        extra_res = client.post(
            f"{path_a}/qr-scan",
            headers=driver_a,
            json={
                "revision": 0,
                "rawPayload": raw_secret_payload,
                "fields": {"id_number": "999999999999"},
            },
        )
        assert extra_res.status_code == 400

        # Upload cccd_front capture (revision 0 -> 1)
        png_bytes = _encode_png(np.full((60, 100, 3), 255, dtype=np.uint8))
        up = client.put(
            f"{path_a}/captures/cccd_front?revision=0",
            headers={**driver_a, "Content-Type": "image/png"},
            content=png_bytes,
        )
        assert up.status_code == 200
        detail0 = client.get(path_a, headers=driver_a).json()["data"]
        cap_id = detail0["document"]["captures"]["cccd_front"]["id"]

        # Stale revision -> 409
        stale = client.post(
            f"{path_a}/qr-scan",
            headers=driver_a,
            json={"revision": 0, "rawPayload": raw_secret_payload, "documentSide": "FRONT"},
        )
        assert stale.status_code == 409

        # Invalid QR payload returns INVALID_FORMAT without mutating valid state
        bad_qr = client.post(
            f"{path_a}/qr-scan",
            headers=driver_a,
            json={
                "revision": 1,
                "rawPayload": "04609600309||BAD|31021996|Nam|X|01052021",
                "documentSide": "FRONT",
            },
        ).json()["data"]
        assert bad_qr["qr"]["status"] == "INVALID_FORMAT"
        assert bad_qr["revision"] == 1

        # Valid live scan binds to application, revision, attachment, and boundToCapture=False
        scan_res = client.post(
            f"{path_a}/qr-scan",
            headers=driver_a,
            json={
                "revision": 1,
                "rawPayload": raw_secret_payload,
                "documentSide": "FRONT",
                "attachmentId": cap_id,
            },
        ).json()["data"]
        assert scan_res["revision"] == 2
        assert scan_res["qr"]["status"] == "DECODED_VALID"
        assert scan_res["qr"]["source"] == "QR_LIVE_SCAN"
        assert scan_res["qr"]["documentSide"] == "FRONT"
        assert scan_res["qr"]["attachmentId"] == cap_id
        assert scan_res["qr"]["boundToCapture"] is False
        assert scan_res["qr"]["crossValidationStatus"] == "INSUFFICIENT_EVIDENCE"
        codes = {w["code"] for w in scan_res["verificationWarnings"]}
        assert {"QR_OCR_INSUFFICIENT_EVIDENCE", "QR_LIVE_SCAN_UNBOUND"} <= codes

        # Replacing cccd_front invalidates stale qrLiveScan
        png_bytes2 = _encode_png(np.full((60, 100, 3), 200, dtype=np.uint8))
        up2 = client.put(
            f"{path_a}/captures/cccd_front?revision=2",
            headers={**driver_a, "Content-Type": "image/png"},
            content=png_bytes2,
        ).json()["data"]
        assert up2["revision"] == 3
        assert "QR_LIVE_SCAN_UNBOUND" not in {w["code"] for w in up2["verificationWarnings"]}

        # Cross-account duplicate QR detection via HMAC:
        # Driver A submits an application with cccd_id=046096003093; Driver B scans the same QR.
        for slot, rev in (("cccd_back", 3), ("gplx_front", 4), ("gplx_back", 5), ("selfie", 6)):
            client.put(
                f"{path_a}/captures/{slot}?revision={rev}",
                headers={**driver_a, "Content-Type": "image/png"},
                content=png_bytes,
            )
        details_a = {
            "cccd_name": "NGUYỄN VĂN MẪU",
            "cccd_id": "046096003093",
            "cccd_dob": "1996-01-01",
            "cccd_gender": "MALE",
            "gplx_name": "NGUYỄN VĂN MẪU",
            "gplx_number": "250256000001",
            "gplx_dob": "1996-01-01",
            "gplx_classes": ["A1"],
            "no_expiry": True,
            "expiry_date": None,
        }
        assert (
            client.put(
                f"{path_a}/confirmation",
                headers=driver_a,
                json={"revision": 7, "details": details_a},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"{path_a}/submit",
                headers={**driver_a, "Idempotency-Key": "sub-a-1"},
                json={"revision": 8, "warningAcknowledgementRevision": 8},
            ).status_code
            == 200
        )
        app_b = client.post(
            "/ekyc/driver-verification/v1/applications", headers=driver_b
        ).json()["data"]
        dup_scan = client.post(
            f"/ekyc/driver-verification/v1/applications/{app_b['id']}/qr-scan",
            headers=driver_b,
            json={"revision": 0, "rawPayload": raw_secret_payload, "documentSide": "FRONT"},
        ).json()["data"]
        assert any(
            w["code"] == "DUPLICATE_CCCD_ACROSS_ACCOUNTS" and w["blocksActivation"]
            for w in dup_scan["verificationWarnings"]
        )

        # Privacy: neither rawPayload nor id_number in logs
        assert raw_secret_payload not in caplog.text
        assert "046096003093" not in caplog.text
        assert "Địa chỉ riêng" not in caplog.text


def test_gplx_qr_parsing_image_decoding_and_live_scan(settings, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from firebase_admin import auth
    from pydantic import SecretStr

    from ekyc.main import create_app
    from ekyc.pipeline.qr import compare_qr_and_ocr_fields, decode_qr_image, parse_gplx_qr_payload

    gplx_pipe = "790123456789|NGUYỄN VĂN MẪU|01/01/1996|A1, B2|Sở GTVT TP.HCM"
    parsed_pipe = parse_gplx_qr_payload(gplx_pipe, slot="gplx_back", today=TODAY)
    assert parsed_pipe.status == "DECODED_VALID"
    assert parsed_pipe.fields["id_number"] == "790123456789"
    assert parsed_pipe.fields["vehicle_class"] == ["A1", "B2"]

    gplx_multiline = (
        "Họ tên: NGUYỄN VĂN MẪU\n"
        "Ngày sinh: 01/01/1996\n"
        "Hạng: B2\n"
        "Nơi cấp: Sở GTVT Hà Nội"
    )
    parsed_multi = parse_gplx_qr_payload(gplx_multiline, slot="gplx_back", today=TODAY)
    assert parsed_multi.status == "DECODED_VALID"
    assert parsed_multi.fields["vehicle_class"] == ["B2"]
    assert "id_number" in parsed_multi.missing_fields

    assert (
        parse_gplx_qr_payload(
            "https://gplx.gov.vn/default.aspx?s=xyz", slot="gplx_back"
        ).status
        == "UNSUPPORTED_FORMAT"
    )

    canvas = _make_qr_card(gplx_pipe, width=900, height=560, region="gplx_back_left")
    decoded = decode_qr_image(canvas, "gplx_back", warped=True)
    assert decoded.parsed.status == "DECODED_VALID"
    assert decoded.card_variant == "GPLX_PET"
    assert decoded.parsed.fields["vehicle_class"] == ["A1", "B2"]

    class_conflict = compare_qr_and_ocr_fields(
        {"full_name": "NGUYỄN VĂN MẪU", "dob": "1996-01-01", "vehicle_class": ["A1", "B2"]},
        {"full_name": "NGUYỄN VĂN MẪU", "dob": "1996-01-01", "vehicle_class": ["A1"]},
    )
    assert class_conflict.status == "CONFLICT"
    assert class_conflict.conflict_fields == ("vehicle_class",)

    project = "synthetic-project"
    monkeypatch.setattr(
        auth,
        "verify_id_token",
        lambda token, *, app, check_revoked: {
            "sub": token,
            "aud": project,
            "iss": f"https://securetoken.google.com/{project}",
            "driver_verification_admin": False,
        },
    )
    app = create_app(
        settings.model_copy(
            update={
                "firebase_project_id": project,
                "verification_db_path": tmp_path / "gplx_qr.sqlite3",
                "verification_lookup_hmac_key": SecretStr("unit-test-hmac-secret-32-chars!!"),
            }
        )
    )
    driver = {"Authorization": "Bearer driver-g"}
    with TestClient(app) as client:
        created = client.post(
            "/ekyc/driver-verification/v1/applications", headers=driver
        ).json()["data"]
        path = f"/ekyc/driver-verification/v1/applications/{created['id']}"
        detail = client.get(path, headers=driver).json()["data"]
        assert any(
            w["code"] == "DOCUMENT_REQUIRED" and w["slot"] == "gplx_back"
            for w in detail["verificationWarnings"]
        )
        res = client.post(
            f"{path}/qr-scan",
            headers=driver,
            json={
                "revision": 0,
                "rawPayload": gplx_pipe,
                "documentSide": "BACK",
                "documentType": "GPLX",
            },
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["qr"]["status"] == "DECODED_VALID"
        assert data["qr"]["cardVariant"] == "GPLX_PET"
        assert data["qr"]["fields"]["vehicle_class"] == ["A1", "B2"]
        refreshed = client.get(path, headers=driver).json()["data"]
        assert refreshed["document"]["gplxQrLiveScan"]["fields"]["vehicle_class"] == ["A1", "B2"]
        assert not refreshed["document"].get("qrLiveScan")


def test_cccd_back_ocr_skip_vs_required_and_gplx_glare_4corner_fallback(settings):
    from fastapi.testclient import TestClient

    from ekyc.main import create_app
    from ekyc.pipeline.ocr.base import OcrLine

    # 1. Can Cuoc 2024 on cccd_back with valid issue_date in QR still runs 1 lightweight
    #    classification view (max 960px, not 2 views) and skips redundant field OCR cleanly.
    payload_2024 = "046096003095||TRẦN THỊ MẪU|12/04/1998|Nữ|TP.HCM|15/08/2024"
    card_2024 = _make_qr_card(payload_2024, region="back_mr", blur=False)
    prep = SimpleNamespace(prepare_loaded=lambda loaded, diagnostics=True: PreparedCard(card_2024))
    ocr_calls = 0
    observed_max_sides: list[int] = []

    def classify_probe(img):
        nonlocal ocr_calls
        ocr_calls += 1
        observed_max_sides.append(max(img.shape[:2]))
        return [OcrLine("ĐẶC ĐIỂM NHẬN DẠNG", 0.95, 20, 20, 220, 44)]

    cls_back = SimpleNamespace(
        classify=lambda *_: DocClassification(DocType.CCCD, Side.BACK, 0.9, 0, [], True)
    )
    back_with_qr = DocumentExtractor(
        settings, prep, SimpleNamespace(read_lines=classify_probe), cls_back
    ).extract(_encode_png(card_2024), "cccd_back")
    assert ocr_calls == 1
    assert observed_max_sides == [960]
    assert back_with_qr.ocr_status == "SKIPPED"
    assert "DOCUMENT_SLOT_MISMATCH" not in back_with_qr.warnings
    assert back_with_qr.qr is not None
    assert back_with_qr.qr.status == "DECODED_VALID"
    assert back_with_qr.qr.fields["issue_date"] == "2024-08-15"

    # 1b. Explicitly requested ocr_mode="classify_only" on valid CCCD_BACK early-stops at 1 view.
    ocr_calls = 0
    observed_max_sides.clear()
    blank_back = np.full((600, 950, 3), 235, dtype=np.uint8)
    prep_blank = SimpleNamespace(
        prepare_loaded=lambda loaded, diagnostics=True: PreparedCard(blank_back)
    )
    back_classify_ok = DocumentExtractor(
        settings, prep_blank, SimpleNamespace(read_lines=classify_probe), cls_back
    ).extract(_encode_png(blank_back), "cccd_back", ocr_mode="classify_only")
    assert ocr_calls == 1
    assert back_classify_ok.ocr_status == "SKIPPED"
    assert "DOCUMENT_SLOT_MISMATCH" not in back_classify_ok.warnings

    # 1c. Wrong document type (GPLX_FRONT) uploaded as cccd_back under classify_only -> MISMATCH!
    cls_gplx = SimpleNamespace(
        classify=lambda *_: DocClassification(DocType.GPLX, Side.FRONT, 0.9, 0, [], True)
    )
    wrong_doc = DocumentExtractor(
        settings, prep_blank, SimpleNamespace(read_lines=classify_probe), cls_gplx
    ).extract(_encode_png(blank_back), "cccd_back", ocr_mode="classify_only")
    assert wrong_doc.ocr_status == "OCR_NO_FIELDS"
    assert "DOCUMENT_SLOT_MISMATCH" in wrong_doc.warnings

    # 1d. CCCD 2021 FRONT (WITH valid QR!) mistakenly uploaded into cccd_back -> STILL detects
    #     DOCUMENT_SLOT_MISMATCH under BOTH classify_only and auto-QR fallback!
    front_card = _make_qr_card(payload_2024, region="front_tr", blur=False)
    prep_front = SimpleNamespace(
        prepare_loaded=lambda loaded, diagnostics=True: PreparedCard(front_card)
    )
    cls_cccd_front = SimpleNamespace(
        classify=lambda *_: DocClassification(DocType.CCCD, Side.FRONT, 0.95, 0, [], True)
    )
    for mode in ("classify_only", "full"):
        dup_front = DocumentExtractor(
            settings, prep_front, SimpleNamespace(read_lines=classify_probe), cls_cccd_front
        ).extract(_encode_png(front_card), "cccd_back", ocr_mode=mode)
        assert "DOCUMENT_SLOT_MISMATCH" in dup_front.warnings
        assert dup_front.ocr_status == "OCR_NO_FIELDS"

    # 1e. Blank/unreadable capture under classify_only -> OCR_NO_TEXT (not silent SKIPPED).
    cls_unknown = SimpleNamespace(
        classify=lambda *_: DocClassification(DocType.UNKNOWN, Side.UNKNOWN, 0.0, 0, [], False)
    )
    blank_res = DocumentExtractor(
        settings, prep_blank, SimpleNamespace(read_lines=lambda _: []), cls_unknown
    ).extract(_encode_png(blank_back), "cccd_back", ocr_mode="classify_only")
    assert blank_res.ocr_status == "OCR_NO_TEXT"
    assert "DOCUMENT_SLOT_MISMATCH" not in blank_res.warnings

    # 2. cccd_back WITHOUT QR must STILL execute full EasyOCR and early-stop when issue_date parsed.
    ocr_calls = 0

    def read_with_issue_date(_img):
        nonlocal ocr_calls
        ocr_calls += 1
        return [OcrLine("Ngày cấp: 15/08/2024", 0.95, 20, 40, 260, 65)]

    back_no_qr = DocumentExtractor(
        settings, prep_blank, SimpleNamespace(read_lines=read_with_issue_date), cls_back
    ).extract(_encode_png(blank_back), "cccd_back")
    assert ocr_calls == 1
    assert back_no_qr.ocr_status == "OCR_READ"
    assert back_no_qr.qr is not None
    assert back_no_qr.qr.status == "NOT_FOUND"
    assert any(
        c.field_key == "issue_date" and c.candidate_value == "2024-08-15"
        for c in back_no_qr.candidates
    )

    # Route query param contract: /documents/{slot}/extract?ocr_mode=classify_only vs invalid 400
    route_app = create_app(settings)
    seen_calls: list[tuple[str, str]] = []

    class StubWorker:
        def extract(self, _raw, slot, source="both", *, ocr_mode="full", timeout=None):
            seen_calls.append((slot, ocr_mode))
            from ekyc.schemas.document import ExtractionResult

            return ExtractionResult(slot=slot, ocr_status="SKIPPED")

        def close(self):
            pass

    with TestClient(route_app) as route_client:
        route_app.state.cccd_worker = StubWorker()
        hdrs = {"X-API-Key": settings.ekyc_api_key.get_secret_value()}
        res_ok = route_client.post(
            "/ekyc/documents/cccd_back/extract?ocr_mode=classify_only",
            headers=hdrs,
            content=_encode_png(blank_back),
        )
        assert res_ok.status_code == 200
        assert res_ok.json()["data"]["ocrStatus"] == "SKIPPED"
        assert seen_calls == [("cccd_back", "classify_only")]
        res_bad = route_client.post(
            "/ekyc/documents/cccd_back/extract?ocr_mode=bogus",
            headers=hdrs,
            content=_encode_png(blank_back),
        )
        assert res_bad.status_code == 400

    # 3. All 4 corners + 0/90/180/270 rotations + PET glare rescue + budget cap + multi-QR safety
    gplx_payload = "790123456789|NGUYỄN VĂN MẪU|01/01/1996|A1, B2|Sở GTVT Hà Nội"
    qr = cv2.QRCodeEncoder_create().encode(gplx_payload)
    qr = cv2.copyMakeBorder(qr, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    qr_small = cv2.cvtColor(
        cv2.resize(qr, (130, 130), interpolation=cv2.INTER_AREA), cv2.COLOR_GRAY2BGR
    )
    scene = np.full((1200, 1600, 3), 210, dtype=np.uint8)
    scene[880:1010, 1280:1410] = qr_small  # bottom-right quadrant missed by [:2] before!
    # Apply PET glare over bottom-right QR
    yy, xx = np.mgrid[0:1200, 0:1600].astype(np.float32)
    glare = (np.exp(-((xx - 1350) ** 2 + (yy - 940) ** 2) / (2 * 110 * 110)) * 0.68)[..., None]
    glared = np.clip(scene.astype(np.float32) * (1.0 - glare) + 255.0 * glare, 0, 255).astype(
        np.uint8
    )
    for rot in (None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE):
        frame = glared if rot is None else cv2.rotate(glared, rot)
        decoded = decode_qr_image(frame, "gplx_back", warped=False, try_fallback=True, today=TODAY)
        assert decoded.parsed.status == "DECODED_VALID"
        assert decoded.parsed.fields["vehicle_class"] == ["A1", "B2"]

    # Budget exhaustion on blank image must return NOT_FOUND cleanly without crashing
    empty = np.full((800, 1200, 3), 220, dtype=np.uint8)
    capped = decode_qr_image(
        empty, "gplx_back", warped=False, try_fallback=True, max_attempts=3, time_budget_s=0.05
    )
    assert capped.parsed.status == "NOT_FOUND"

    # gplx_back YOLO warp fallback rescues tilted card when raw pass sees only warped card ROI
    tilted_scene = np.full((1200, 1600, 3), 120, dtype=np.uint8)
    src = np.float32([[0, 0], [900, 0], [900, 560], [0, 560]])
    dst = np.float32([[250, 200], [1200, 350], [1150, 920], [200, 820]])
    warped_card = _make_qr_card(gplx_payload, width=900, height=560, region="gplx_back_left")
    cv2.warpPerspective(
        warped_card,
        cv2.getPerspectiveTransform(src, dst),
        (1600, 1200),
        dst=tilted_scene,
        borderMode=cv2.BORDER_TRANSPARENT,
    )
    gplx_prep = SimpleNamespace(prepare_loaded=lambda loaded: PreparedCard(warped_card))
    gplx_res = DocumentExtractor(
        settings, gplx_prep, SimpleNamespace(read_lines=lambda _: []), cls_back
    ).extract(_encode_png(tilted_scene), "gplx_back")
    assert gplx_res.ocr_status == "OCR_UNSUPPORTED"
    assert gplx_res.qr is not None
    assert gplx_res.qr.status == "DECODED_VALID"
    assert gplx_res.qr.fields["vehicle_class"] == ["A1", "B2"]


def _make_dual_qr_canvas(payload_left: str, payload_right: str) -> np.ndarray:
    canvas = np.full((1000, 1600, 3), 235, dtype=np.uint8)
    for x0, payload in ((160, payload_left), (960, payload_right)):
        qr = cv2.QRCodeEncoder_create().encode(payload)
        qr = cv2.copyMakeBorder(qr, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
        qr_bgr = cv2.cvtColor(
            cv2.resize(qr, (260, 260), interpolation=cv2.INTER_NEAREST),
            cv2.COLOR_GRAY2BGR,
        )
        canvas[240:500, x0 : x0 + 260] = qr_bgr
    return canvas


def test_multi_qr_distinct_identities_conflict_and_unified_budget(settings, monkeypatch):
    from ekyc.pipeline import qr as qr_mod
    from ekyc.services import document_extraction as de_mod

    cls_front = SimpleNamespace(
        classify=lambda *_: DocClassification(DocType.CCCD, Side.FRONT, 0.9, 0, [], True)
    )
    ocr_stub = SimpleNamespace(read_lines=lambda _: [])
    prep_stub = SimpleNamespace(
        prepare_loaded=lambda loaded, diagnostics=True: PreparedCard(loaded.bgr)
    )

    # 1. Two distinct CCCD identities -> CONFLICT, empty fields, MULTIPLE_DISTINCT_QR_IDENTITIES
    cccd_a = "001095001111||NGUYỄN VĂN A|01011995|Nam|HN|01052021"
    cccd_b = "001096002222||TRẦN THỊ B|02021996|Nữ|HCM|15082024"
    dual_cccd = _make_dual_qr_canvas(cccd_a, cccd_b)
    decoded_cccd = decode_qr_image(dual_cccd, "cccd_front", warped=False, try_fallback=True)
    assert decoded_cccd.parsed.status == "CONFLICT"
    assert decoded_cccd.parsed.fields == {}
    assert "MULTIPLE_DISTINCT_QR_IDENTITIES" in decoded_cccd.parsed.warnings
    assert {"id_number", "full_name", "dob"} <= set(decoded_cccd.parsed.conflict_fields)

    res_cccd = DocumentExtractor(settings, prep_stub, ocr_stub, cls_front).extract(
        _encode_png(dual_cccd), "cccd_front"
    )
    assert res_cccd.qr is not None
    assert res_cccd.qr.status == "CONFLICT"
    assert res_cccd.qr.fields == {}
    assert res_cccd.qr.bound_to_capture is False
    assert res_cccd.qr.cross_validation_status == "CONFLICT"
    assert "MULTIPLE_DISTINCT_QR_IDENTITIES" in res_cccd.warnings
    assert "MULTIPLE_DISTINCT_QR_IDENTITIES" in res_cccd.qr.warnings
    assert {"id_number", "full_name", "dob"} <= set(res_cccd.conflict_fields)
    assert not any(c.source == "QR" for c in res_cccd.candidates)

    # 2. Two distinct GPLX identities -> CONFLICT, MULTIPLE_DISTINCT_QR_IDENTITIES
    gplx_a = "790123456789|NGUYỄN VĂN MẪU|01/01/1996|A1|Sở GTVT Hà Nội"
    gplx_b = "790987654321|TRẦN VĂN KHÁC|05/05/1992|B2|Sở GTVT TP.HCM"
    dual_gplx = _make_dual_qr_canvas(gplx_a, gplx_b)
    res_gplx = DocumentExtractor(settings, prep_stub, ocr_stub, cls_front).extract(
        _encode_png(dual_gplx), "gplx_back"
    )
    assert res_gplx.qr is not None
    assert res_gplx.qr.status == "CONFLICT"
    assert res_gplx.qr.bound_to_capture is False
    assert "MULTIPLE_DISTINCT_QR_IDENTITIES" in res_gplx.warnings
    assert not any(c.source == "QR" for c in res_gplx.candidates)

    # 3. Valid GPLX QR + unsupported URL QR -> no conflict, DECODED_VALID
    mixed_gplx = _make_dual_qr_canvas("https://gplx.gov.vn/default.aspx?s=xyz", gplx_a)
    decoded_mixed = decode_qr_image(mixed_gplx, "gplx_back", warped=False, try_fallback=True)
    assert decoded_mixed.parsed.status == "DECODED_VALID"
    assert decoded_mixed.parsed.fields["id_number"] == "790123456789"

    # 4. Two identical valid CCCD QRs -> no conflict, DECODED_VALID
    same_cccd = _make_dual_qr_canvas(cccd_a, cccd_a)
    decoded_same = decode_qr_image(same_cccd, "cccd_front", warped=False, try_fallback=True)
    assert decoded_same.parsed.status == "DECODED_VALID"
    assert decoded_same.parsed.fields["id_number"] == "001095001111"

    # 5. Unified QrDecodeBudget is shared across extract() passes and stops YOLO warp if exhausted
    seen_budgets: list[tuple[int, int]] = []
    yolo_warp_calls = 0

    def spy_decode(image, slot, **kwargs):
        b = kwargs.get("budget")
        assert b is not None
        seen_budgets.append((id(b), b.attempts))
        b.attempts = b.max_attempts  # exhaust on first pass
        return qr_mod.QrDecodeResult(
            parsed=qr_mod.QrParsedResult(status="NOT_FOUND", legacy_status="QR_NOT_FOUND"),
            document_side="BACK",
        )

    def spy_warp(_loaded):
        nonlocal yolo_warp_calls
        yolo_warp_calls += 1
        return PreparedCard(np.full((400, 600, 3), 210, dtype=np.uint8))

    monkeypatch.setattr(de_mod, "decode_qr_image", spy_decode)
    extractor = DocumentExtractor(settings, prep_stub, ocr_stub, cls_front)
    monkeypatch.setattr(extractor, "_try_prepare_for_gplx_qr", spy_warp)
    blank_png = _encode_png(np.full((400, 600, 3), 220, dtype=np.uint8))
    extractor.extract(blank_png, "gplx_back")
    assert len(seen_budgets) == 1
    assert yolo_warp_calls == 0

    # Also verify shared budget object identity when pass 1 consumes partial attempts
    seen_partial: list[tuple[int, int]] = []

    def partial_decode(image, slot, **kwargs):
        b = kwargs.get("budget")
        assert b is not None
        seen_partial.append((id(b), b.attempts))
        b.attempts += 5
        return qr_mod.QrDecodeResult(
            parsed=qr_mod.QrParsedResult(status="NOT_FOUND", legacy_status="QR_NOT_FOUND"),
            document_side="BACK",
        )

    monkeypatch.setattr(de_mod, "decode_qr_image", partial_decode)
    extractor.extract(blank_png, "gplx_back")
    assert len(seen_partial) == 3
    assert len({item[0] for item in seen_partial}) == 1
    assert [item[1] for item in seen_partial] == [0, 5, 10]
