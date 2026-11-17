import base64
from pathlib import Path

from ekyc.deps import get_cccd_service
from ekyc.services.face_comparison import FaceResult, PairResult

PATH = "/ekyc/face/evaluate"
HEADERS = {"X-API-Key": "test-key"}
SYNTHETIC_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nsynthetic").decode("ascii")


def test_face_evaluate_defaults_to_disabled_and_requires_api_key(client):
    assert client.post(PATH, json={"images": {"selfie": SYNTHETIC_B64}}).status_code == 401

    response = client.post(
        PATH,
        json={"quality_only": True, "images": {"selfie": SYNTHETIC_B64}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()["data"]
    assert data["mode"] == "NOT_EVALUATED"
    assert data["reason"] == "DISABLED"
    assert data["liveness"] == "NOT_EVALUATED"


def test_face_evaluate_bounds_streamed_json_before_validation(app, client):
    app.state.settings = app.state.settings.model_copy(update={"max_image_bytes": 100})
    headers = {**HEADERS, "Content-Type": "application/json"}
    chunks = iter([b'{"images":{"selfie":"', b"A" * 5000, b'"}}'])
    response = client.post(PATH, content=chunks, headers=headers)
    assert response.status_code == 413
    assert response.json()["errorCode"] == "IMAGE_TOO_LARGE"


def test_face_evaluate_validates_payload_and_delegates_when_enabled(app, client, tmp_path: Path):
    invalid = client.post(
        PATH,
        json={"images": {"selfie": "not-valid-base64!!!"}},
        headers=HEADERS,
    )
    assert invalid.json()["errorCode"] == "INVALID_IMAGE"

    manifest = tmp_path / "face_manifest.json"
    manifest.write_text("{}", encoding="utf-8")

    class FakeWorker:
        def check_face_quality(self, images):
            assert list(images) == ["selfie"]
            return FaceResult(
                mode="EXPERIMENTAL",
                reason="QUALITY_ONLY",
                quality={"selfie": "READY"},
                pairs=[
                    PairResult(left="cccd_front", right="selfie", status="NOT_EVALUATED"),
                    PairResult(left="gplx_front", right="selfie", status="NOT_EVALUATED"),
                    PairResult(left="cccd_front", right="gplx_front", status="NOT_EVALUATED"),
                ],
            )

        def compare_faces(self, images):
            assert set(images) == {"cccd_front", "selfie"}
            return FaceResult(
                mode="EXPERIMENTAL",
                modelVersion="test-sface",
                quality={"cccd_front": "READY", "selfie": "READY"},
                pairs=[
                    PairResult(
                        left="cccd_front", right="selfie", status="INCONCLUSIVE", score=0.72
                    ),
                ],
            )

    app.state.settings = app.state.settings.model_copy(
        update={
            "face_comparison_enabled": True,
            "face_comparison_manifest": manifest,
            "ocr_backend": "easyocr",
        }
    )
    app.dependency_overrides[get_cccd_service] = FakeWorker

    q_resp = client.post(
        PATH,
        json={"quality_only": True, "images": {"selfie": SYNTHETIC_B64}},
        headers=HEADERS,
    )
    assert q_resp.status_code == 200
    assert q_resp.headers["cache-control"] == "no-store"
    assert q_resp.json()["data"]["reason"] == "QUALITY_ONLY"
    assert q_resp.json()["data"]["quality"] == {"selfie": "READY"}

    c_resp = client.post(
        PATH,
        json={
            "quality_only": False,
            "images": {"cccd_front": SYNTHETIC_B64, "selfie": SYNTHETIC_B64},
        },
        headers=HEADERS,
    )
    assert c_resp.status_code == 200
    assert c_resp.json()["data"]["mode"] == "EXPERIMENTAL"
    assert c_resp.json()["data"]["pairs"][0]["score"] == 0.72
