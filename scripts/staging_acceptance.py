"""Offline acceptance smoke. Run in a fresh staging container; uses synthetic identities."""

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from time import perf_counter
from unittest.mock import patch

import cv2
import numpy as np
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from firebase_admin import auth

from ekyc.config import Settings
from ekyc.main import create_app

ROOT = "/ekyc/driver-verification/v1"
PROJECT = "synthetic-acceptance"
DETAILS = {
    "cccd_name": "TEN MAU A",
    "cccd_id": "000000000001",
    "cccd_dob": "1990-02-01",
    "cccd_gender": "MALE",
    "gplx_name": "TEN MAU A",
    "gplx_number": "000000000002",
    "gplx_dob": "1990-02-01",
    "gplx_classes": ["A1", "B"],
    "expiry_date": None,
    "no_expiry": True,
}
FIELD_GT = {
    "cccd_front": {
        "full_name": DETAILS["cccd_name"],
        "id_number": DETAILS["cccd_id"],
        "dob": DETAILS["cccd_dob"],
    },
    "gplx_front": {
        "full_name": DETAILS["gplx_name"],
        "id_number": DETAILS["gplx_number"],
        "dob": DETAILS["gplx_dob"],
    },
}


def verify(token, *, app, check_revoked):
    assert app.project_id == PROJECT and check_revoked
    if token == "expired":
        raise auth.ExpiredIdTokenError("synthetic expiry", None)
    assert token in {"driver", "admin", "other"}
    return {
        "sub": token,
        "aud": PROJECT,
        "iss": f"https://securetoken.google.com/{PROJECT}",
        "driver_verification_admin": token == "admin",
    }


def frame(slot):
    image = np.full((350, 560, 3), 225, dtype=np.uint8)
    if slot == "cccd_front":
        qr = cv2.QRCodeEncoder_create().encode(
            "000000000001||TEN MAU A|01021990|Nam|SYNTHETIC|01012021"
        )
        qr = cv2.resize(qr, None, fx=4, fy=4, interpolation=cv2.INTER_NEAREST)
        height, width = qr.shape
        image[20 : 20 + height, 360 : 360 + width] = qr[:, :, None]
        rows = ("CAN CUOC CONG DAN", "So: 000000000001", "Ho va ten: TEN MAU A")
    elif slot == "gplx_front":
        rows = (
            "GIAY PHEP LAI XE",
            "So: 000000000002",
            "Ho ten: TEN MAU A",
            "Ngay sinh: 01/02/1990",
            "Hang: A1, B",
        )
    else:
        rows = ()
    for index, label in enumerate(rows):
        cv2.putText(
            image, label, (20, 58 + index * 47), cv2.FONT_HERSHEY_SIMPLEX, 0.70, (8, 8, 8), 2
        )
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return encoded.tobytes()


def call(client, method, path, *, actor="driver", **kwargs):
    started = perf_counter()
    headers = {"Authorization": f"Bearer {actor}", **kwargs.pop("headers", {})}
    response = client.request(method, path, headers=headers, **kwargs)
    return response, round((perf_counter() - started) * 1000, 2)


def data(response):
    value = response.json()
    assert response.status_code == 200 and value["success"], (
        response.status_code,
        value.get("errorCode"),
    )
    return value["data"]


def workflow(client):
    assert data(call(client, "GET", ROOT + "/me")[0])
    assert call(client, "GET", ROOT + "/me", actor="expired")[0].status_code == 401
    record = data(call(client, "POST", ROOT + "/applications")[0])
    path = ROOT + "/applications/" + record["id"]
    admin = ROOT + "/admin/applications/" + record["id"]
    assert call(client, "GET", path, actor="other")[0].status_code == 404
    assert call(client, "GET", admin)[0].status_code == 403
    timings = {"upload_ms": [], "extract_api_ms": []}
    for slot in ("cccd_front", "cccd_back", "gplx_front", "selfie"):
        response, elapsed = call(
            client,
            "PUT",
            path + f"/captures/{slot}?revision={record['revision']}",
            content=frame(slot),
            headers={"Content-Type": "image/png"},
        )
        record = data(response)
        timings["upload_ms"].append(elapsed)
    assert call(client, "GET", path + "/captures/cccd_front", actor="other")[0].status_code == 404

    by_source = {"QR": {}, "OCR": {}}
    statuses = {}
    for slot, gt in FIELD_GT.items():
        response, elapsed = call(
            client, "POST", path + "/extract/" + slot, json={"revision": record["revision"]}
        )
        record = data(response)
        proposal = record["proposal"]
        timings["extract_api_ms"].append(elapsed)
        statuses[slot] = [proposal["qrStatus"], proposal["ocrStatus"]]
        for item in proposal["candidates"]:
            field = item["fieldKey"]
            if field in gt and item["validationStatus"] == "valid":
                by_source[item["source"]][slot, field] = item["candidateValue"]

    assert statuses["cccd_front"][0] == "QR_FOUND_SUPPORTED"
    assert statuses["gplx_front"][0] == "QR_NOT_FOUND"
    eligible = {
        (slot, field): value for slot, gt in FIELD_GT.items() for field, value in gt.items()
    }
    qr_values = by_source["QR"]
    assert qr_values["cccd_front", "id_number"] == DETAILS["cccd_id"]
    proposed_name = qr_values["cccd_front", "full_name"]
    entered = {**DETAILS, "cccd_id": qr_values["cccd_front", "id_number"]}
    # Simulate correcting a stale draft after reviewing the source on screen.
    entered["cccd_name"] = proposed_name.lower()
    assert entered["cccd_name"] != DETAILS["cccd_name"]
    entered["cccd_name"] = DETAILS["cccd_name"]

    record = data(
        call(
            client,
            "PUT",
            path + "/confirmation",
            json={"revision": record["revision"], "details": entered},
        )[0]
    )
    submission = {
        "revision": record["revision"],
        "warningAcknowledgementRevision": record["revision"],
    }
    idempotency = {"Idempotency-Key": "synthetic-acceptance"}
    record = data(call(client, "POST", path + "/submit", json=submission, headers=idempotency)[0])
    assert record["status"] == "SUBMITTED"
    assert (
        data(call(client, "POST", path + "/submit", json=submission, headers=idempotency)[0])
        == record
    )
    assert data(call(client, "GET", admin, actor="admin")[0])
    preview = call(client, "GET", admin + "/captures/selfie", actor="admin")[0]
    assert preview.status_code == 200 and "no-store" in preview.headers["cache-control"]
    revision = record["revision"]
    record = data(
        call(
            client,
            "POST",
            admin + "/decision",
            actor="admin",
            json={"revision": revision, "status": "UNDER_REVIEW"},
        )[0]
    )
    assert (
        call(
            client,
            "POST",
            admin + "/decision",
            actor="admin",
            json={"revision": revision, "status": "APPROVED"},
        )[0].status_code
        == 409
    )
    assert (
        data(
            call(
                client,
                "POST",
                admin + "/decision",
                actor="admin",
                json={"revision": record["revision"], "status": "APPROVED"},
            )[0]
        )["status"]
        == "APPROVED"
    )
    assert data(call(client, "GET", path)[0])["status"] == "APPROVED"
    ocr = by_source["OCR"]
    return {
        **timings,
        "statuses": statuses,
        "ocr_eligible": len(eligible),
        "ocr_exact": sum(value == eligible[key] for key, value in ocr.items()),
        "ocr_wrong_emitted": sum(value != eligible[key] for key, value in ocr.items()),
        "ocr_missing": len(eligible) - len(ocr),
        "submitted": 1,
        "approved": 1,
        "scripted_corrections": 1,
    }


def concurrent_extracts(client, api_key):
    start = Barrier(3)
    image = frame("gplx_front")

    def request(_):
        start.wait(timeout=5)
        response, elapsed = call(
            client,
            "POST",
            "/ekyc/documents/gplx_front/extract",
            content=image,
            headers={"X-API-Key": api_key, "Content-Type": "image/png"},
        )
        return [response.status_code, response.json().get("errorCode"), elapsed]

    with ThreadPoolExecutor(3) as pool:
        responses = list(pool.map(request, range(3)))
    assert any(status == 200 for status, _, _ in responses)
    assert any(code == "ENGINE_BUSY" for _, code, _ in responses)
    return sorted(responses, key=lambda row: (row[0], row[2]))


def main():
    with tempfile.TemporaryDirectory() as folder:
        settings = Settings(
            _env_file=None,
            firebase_project_id=PROJECT,
            verification_db_path=Path(folder) / "acceptance.sqlite3",
            verification_storage_key=Fernet.generate_key().decode(),
        )
        assert settings.env == "staging" and settings.inference_warm_up
        key = settings.ekyc_api_key.get_secret_value()
        with patch.object(auth, "verify_id_token", side_effect=verify):
            with TestClient(create_app(settings), base_url="https://testserver") as client:
                assert client.get("/health/ready").json()["worker"] == "warm"
                result = workflow(client)
                result["concurrent_requests"] = concurrent_extracts(client, key)
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
