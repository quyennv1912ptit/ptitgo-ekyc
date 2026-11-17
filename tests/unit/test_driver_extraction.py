import io
import json
from uuid import uuid4

import cv2
import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from ekyc.driver_demo import REQUIRED, create_app
from ekyc.driver_extraction import parse_qr, suggestions, worker

PAYLOAD = "000000000001||Nguyễn Văn Mẫu|01011990|Nam|Địa chỉ mẫu|01012020"


def test_qr_decodes_synthetic_image():
    qr = cv2.QRCodeEncoder_create().encode(PAYLOAD)
    qr = cv2.copyMakeBorder(qr, 8, 8, 8, 8, cv2.BORDER_CONSTANT, value=255)
    image = cv2.resize(qr, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
    ok, data = cv2.imencode(".png", image)
    assert ok
    status, fields = worker(data.tobytes(), "cccd_back", "qr")
    assert status == "QR_FOUND_SUPPORTED"
    assert fields["cccd_id"] == "000000000001"
    assert parse_qr(PAYLOAD, "gplx_back") == ("QR_FOUND_UNSUPPORTED", {})
    assert parse_qr("https://example.invalid/", "cccd_back") == ("QR_FOUND_UNSUPPORTED", {})
    assert parse_qr("", "cccd_back") == ("QR_NOT_FOUND", {})


def test_conflict_keeps_both_sources():
    result = suggestions(
        [
            {
                "qr": {"values": {"cccd_name": "Tên Có Dấu"}},
                "ocr": {"values": {"cccd_name": "Ten Co Dau"}},
            }
        ]
    )["cccd_name"]
    assert result["suggested_value"] is None
    assert result["qr_value"] == "Tên Có Dấu"
    assert result["ocr_value"] == "Ten Co Dau"
    assert result["extraction_status"] == "CONFLICT"


def test_extraction_auth_timeout_and_revision_history(tmp_path, monkeypatch):
    from ekyc.core.errors import EkycError, ErrorCode
    from ekyc.schemas.document import ExtractionResult
    from ekyc.services.document_extraction import make_candidate

    def extract(self, raw, slot, source, *, timeout):
        if slot == "cccd_back":
            raise EkycError(ErrorCode.ENGINE_TIMEOUT)
        result = ExtractionResult(
            slot=slot, qr_status="QR_FOUND_SUPPORTED", ocr_status="OCR_READ"
        )
        if slot == "cccd_front":
            value = "QR synthetic" if source == "qr" else "OCR synthetic"
        elif source == "ocr":
            value = "Tên Mâu"
        else:
            return result
        result.candidates.append(make_candidate(slot, "full_name", value, source.upper()))
        return result

    monkeypatch.setattr("ekyc.driver_demo.CccdWorker.extract", extract)
    from itertools import count
    from types import SimpleNamespace

    monkeypatch.setattr(
        "ekyc.driver_demo.time", SimpleNamespace(monotonic=count(1000, 10).__next__)
    )
    driver = {"Authorization": "Bearer " + "d" * 32}
    admin = {"Authorization": "Bearer " + "a" * 32}
    client = TestClient(
        create_app(
            tmp_path / "demo.db",
            {
                "d" * 32: ("driver", "driver"),
                "a" * 32: ("admin", "admin"),
                "o" * 32: ("other", "driver"),
            },
        )
    )
    path = "/api/applications/" + str(uuid4())
    client.put(path, headers=driver)
    buf = io.BytesIO()
    Image.new("RGB", (32, 24)).save(buf, format="PNG")
    data = buf.getvalue()
    headers = {"Content-Type": "image/png", "X-Synthetic-Demo": "confirmed"}
    assert (
        client.post(path + "/extract/cccd_front", headers=headers, content=data).status_code == 401
    )
    other = {**headers, "Authorization": "Bearer " + "o" * 32}
    assert client.post(path + "/extract/cccd_front", headers=other, content=data).status_code == 404
    assert (
        client.post(
            path + "/extract/cccd_front",
            headers={
                **headers,
                **admin,
            },
            content=data,
        ).status_code
        == 403
    )
    assert client.post(
        path + "/extract/cccd_front",
        headers={
            **headers,
            **driver,
        },
        content=data,
    ).json()["ocr"]["values"] == {"cccd_name": "OCR synthetic"}
    assert (
        client.post(
            path + "/extract/cccd_back",
            headers={
                **headers,
                **driver,
            },
            content=data,
        ).json()["ocr"]["status"]
        == "OCR_TIMEOUT"
    )
    # An observation on a different image must not attach to the submitted slot.
    assert (
        client.post(
            path + "/extract/gplx_front",
            headers={
                **headers,
                **driver,
            },
            content=data,
        ).status_code
        == 200
    )
    revised = io.BytesIO()
    Image.new("RGB", (32, 24), "white").save(revised, format="PNG")
    files = {slot: ("fake.png", data, "image/png") for slot in REQUIRED}
    files["gplx_front"] = ("replacement.png", revised.getvalue(), "image/png")
    payload = {
        "revision": 0,
        "confirmed": True,
        "synthetic_only": True,
        "details": dict(
            cccd_name="Edited synthetic",
            cccd_id="000000000001",
            cccd_dob="1990-01-01",
            gplx_number="DEMO",
            gplx_name="Other synthetic",
            gplx_dob="1990-01-01",
            gplx_classes=["A1"],
            gplx_expiry="NO_EXPIRY",
        ),
    }
    response = client.post(
        path + "/submit",
        files=files,
        data={"data": json.dumps(payload)},
        headers={**driver, "Idempotency-Key": "first"},
    )
    assert response.status_code == 200
    review = client.get(path, headers=admin).json()["extraction_history"][0]
    assert review["fields"]["cccd_name"]["edited_by_user"]
    assert "OCR_REVIEW_REQUIRED" in review["fields"]["cccd_name"]["warning_codes"]
    assert review["fields"]["cccd_name"]["confirmed_value"] == "Edited synthetic"
    assert review["fields"]["cccd_name"]["extraction_status"] == "CONFLICT"
    assert review["observations"]["cccd_back"]["ocr"]["status"] == "OCR_TIMEOUT"
    assert "gplx_front" not in review["observations"]
    assert review["cross_document_conflicts"] == ["NAME_MISMATCH"]
    assert (
        client.post(
            path + "/extract/cccd_front",
            headers={
                **headers,
                **driver,
            },
            content=data,
        ).status_code
        == 409
    )

    decision = client.post(
        path + "/decision",
        headers=admin,
        json={
            "action": "NEEDS_RESUBMISSION",
            "reason": "Synthetic review",
            "revision": 1,
        },
    )
    assert decision.status_code == 200
    assert (
        client.post(
            path + "/extract/gplx_front", headers={**headers, **driver}, content=revised.getvalue()
        ).status_code
        == 200
    )
    payload["revision"] = 1
    payload["details"].update(cccd_name="Tên Mẫu", gplx_name="Tên Mẫu")
    payload["name_copy_confirmation"] = "Tên Mâu"
    response = client.post(
        path + "/submit",
        files=files,
        data={"data": json.dumps(payload)},
        headers={**driver, "Idempotency-Key": "second"},
    )
    assert response.status_code == 200
    detail = client.get(path, headers=admin).json()
    review = detail["extraction_history"][-1]
    field = review["fields"]["gplx_name"]
    assert field["ocr_value"] == "Tên Mâu"
    assert field["confirmed_value"] == "Tên Mẫu"
    assert field["edited_by_user"]
    assert review["cross_document_conflicts"] == []
    assert review["original_warning_codes"] == ["DIACRITIC_MISMATCH"]
    assert any(row["action"] == "COPY_CCCD_NAME_CONFIRMED" for row in detail["history"])


def test_unreadable_qr():
    image = np.zeros((48, 48), np.uint8)
    _, data = cv2.imencode(".png", image)
    assert worker(data.tobytes(), "cccd_front", "qr") == ("QR_NOT_FOUND", {})
    assert worker(b"invalid", "cccd_front", "qr") == ("QR_DECODE_FAILED", {})


def test_sideways_ocr_tries_both_directions():
    from ekyc.driver_extraction import read_oriented

    upright = np.arange(24, dtype=np.uint8).reshape(4, 6)
    for rotation in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
        sideways = cv2.rotate(upright, rotation)

        def read(frame):
            if np.array_equal(frame, upright):
                return "OCR_READ", {"gplx_number": "000000000001"}
            return "OCR_NO_FIELDS", {}

        assert read_oriented(sideways, read) == ("OCR_READ", {"gplx_number": "000000000001"})


def test_gplx_parser_diagnostics_and_accent_preservation():
    from types import SimpleNamespace

    from ekyc.config import Settings
    from ekyc.driver_extraction import parse_frame
    from ekyc.pipeline.ocr.base import OcrLine

    settings = Settings(_env_file=None, ekyc_api_key="demo")
    name = OcrLine("Họ tên: Trần Ánh Dương", 0.9, 0, 0, 100, 10)
    for text, expected, value in [
        ("Ngày hết hạn: 31/12/2030", "READ", "2030-12-31"),
        ("Ngày hết hạn: Không thời hạn", "NO_EXPIRY", "NO_EXPIRY"),
        ("Ngày hết hạn: 31/02/2030", "VALIDATION_FAILED", None),
        ("Ngày hết hạn", "PARSE_FAILED", None),
        ("Dòng khác", "OCR_MISSING", None),
    ]:
        expiry = OcrLine(text, 0.9, 0, 60, 100, 70)
        diagnostics = {}
        engine = SimpleNamespace(read_lines=lambda _, expiry=expiry: [name, expiry])
        _, fields = parse_frame(None, "gplx_front", engine, settings, diagnostics)
        assert fields["gplx_name"] == "TRẦN ÁNH DƯƠNG"
        expected_value = (
            None
            if value is None
            else {
                "no_expiry": value == "NO_EXPIRY",
                "expiry_date": None if value == "NO_EXPIRY" else value,
            }
        )
        assert fields.get("gplx_expiry") == expected_value
        assert diagnostics["gplx_expiry"] == expected


def test_expiry_contract_and_conflicting_sources():
    from ekyc.driver_demo import Details, conflict_code
    from ekyc.driver_extraction import suggestions

    common = dict(
        cccd_name="Tên Mẫu",
        cccd_id="000000000001",
        cccd_dob="1990-01-01",
        gplx_name="Tên Mẫu",
        gplx_number="DEMO",
        gplx_classes=["A1"],
        gplx_dob="1990-01-01",
    )
    indefinite = {"no_expiry": True, "expiry_date": None}
    dated = {"no_expiry": False, "expiry_date": "2030-12-31"}
    assert Details.model_validate({**common, **indefinite}).valid()
    assert Details.model_validate({**common, **dated}).valid()
    assert not Details.model_validate({**common, **dated, "no_expiry": True}).valid()
    assert not Details.model_validate({**common, **indefinite, "no_expiry": False}).valid()
    result = suggestions(
        [
            {
                "qr": {"values": {"gplx_expiry": dated}},
                "ocr": {"values": {"gplx_expiry": indefinite}},
            }
        ]
    )["gplx_expiry"]
    assert result["suggested_value"] is None
    assert result["ocr_value"] == indefinite
    assert conflict_code({**common, "gplx_name": "Ten Mau"}) == "DIACRITIC_MISMATCH"
