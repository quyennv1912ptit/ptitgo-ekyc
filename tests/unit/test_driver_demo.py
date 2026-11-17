import io
import json
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from ekyc.driver_demo import LIMIT, REQUIRED, create_app

DRIVER = {"Authorization": "Bearer " + "d" * 32}
OTHER = {"Authorization": "Bearer " + "o" * 32}
ADMIN = {"Authorization": "Bearer " + "a" * 32}
DETAILS = dict(
    cccd_name="Synthetic Driver",
    cccd_id="000000000001",
    cccd_dob="1990-01-01",
    gplx_number="SYNTHETIC",
    gplx_name="Synthetic Driver",
    gplx_dob="1990-01-01",
    gplx_classes=["A1", "B2"],
    gplx_expiry="NO_EXPIRY",
)


def test_driver_to_admin_workflow(tmp_path, caplog):
    client = TestClient(
        create_app(
            tmp_path / "demo.db",
            {
                "d" * 32: ("one", "driver"),
                "o" * 32: ("two", "driver"),
                "a" * 32: ("admin", "admin"),
            },
        )
    )
    path = "/api/applications/" + str(uuid4())
    assert client.put(path).status_code == 401
    assert client.put(path, headers=DRIVER).json()["status"] == "DRAFT"
    assert client.get(path, headers=OTHER).status_code == 404
    assert client.put(path, headers=OTHER).status_code == 404
    assert (
        client.post(
            path + "/decision",
            headers=DRIVER,
            json={
                "action": "APPROVED",
                "revision": 1,
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            path + "/decision",
            headers=ADMIN,
            json={
                "action": "APPROVED",
                "revision": 0,
            },
        ).status_code
        == 409
    )

    image = io.BytesIO()
    Image.new("RGB", (24, 16)).save(image, format="PNG")
    images = {slot: ("../../ignored.png", image.getvalue(), "image/png") for slot in REQUIRED}

    def submit(key, revision, files):
        return client.post(
            path + "/submit",
            headers={**DRIVER, "Idempotency-Key": key},
            files=files,
            data={
                "data": json.dumps(
                    {
                        "revision": revision,
                        "details": DETAILS,
                        "confirmed": True,
                        "synthetic_only": True,
                    }
                )
            },
        )

    assert submit("missing", 0, {"selfie": images["selfie"]}).status_code == 422
    for bad, code in [(b"not an image", 422), (b"x" * (LIMIT + 1), 413)]:
        assert submit("bad", 0, {"selfie": ("x", bad, "image/png")}).status_code == code
    assert submit("first", 0, images).json()["status"] == "PENDING_REVIEW"
    assert submit("first", 0, images).json()["revision"] == 1
    assert submit("different", 0, images).status_code == 409
    assert client.get("/api/applications", headers=ADMIN).json()[0]["revision"] == 1
    assert client.get(path, headers=ADMIN).json()["details"]["gplx_classes"] == ["A1", "B2"]
    photo = path + "/captures/selfie"
    assert client.get(photo).status_code == 401
    assert client.get(photo, headers=OTHER).status_code == 404
    assert client.get(photo, headers=ADMIN).content == image.getvalue()
    assert client.get(photo, headers=ADMIN).headers["cache-control"] == "no-store"
    assert client.get("/ignored.png").status_code == 404

    def decide(action, revision, reason=""):
        return client.post(
            path + "/decision",
            headers=ADMIN,
            json={
                "action": action,
                "revision": revision,
                "reason": reason,
            },
        )

    assert decide("REJECTED", 1).status_code == 422
    assert decide("NEEDS_RESUBMISSION", 1, "Synthetic retake").status_code == 200
    assert client.get(path, headers=DRIVER).json()["status"] == "NEEDS_RESUBMISSION"
    assert submit("second", 1, {"selfie": images["selfie"]}).json()["revision"] == 2
    assert decide("APPROVED", 1).status_code == 409
    assert decide("APPROVED", 2).json()["status"] == "APPROVED"
    assert decide("REJECTED", 2, "Late").status_code == 409

    path = "/api/applications/" + str(uuid4())
    client.put(path, headers=DRIVER)
    assert submit("reject-demo", 0, images).status_code == 200
    assert decide("REJECTED", 1, "Synthetic rejection").json()["status"] == "REJECTED"
    assert "Synthetic Driver" not in caplog.text
