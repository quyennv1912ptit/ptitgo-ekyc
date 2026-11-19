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
