import hashlib
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from ekyc.config import Settings
from ekyc.core.errors import ErrorCode
from ekyc.core.operations_metrics import RuntimeMetrics
from ekyc.main import create_app
from ekyc.services import model_artifacts

STAGING_KEY = "synthetic-test-key-for-staging-only"


def staging_settings(**overrides):
    return Settings(
        _env_file=None,
        **{"env": "staging", "ekyc_api_key": STAGING_KEY, "ocr_backend": "none", **overrides},
    )


@pytest.mark.parametrize("env", ["staging", "prod"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"ekyc_api_key": "short"},
        {"inference_ocr_factory": "tests.fake:factory"},
        {"card_experimental_keypoint_recovery": True},
    ],
)
def test_deployed_settings_reject_unsafe_defaults(env, overrides):
    with pytest.raises(ValueError) as error:
        staging_settings(env=env, **overrides)
    assert STAGING_KEY not in str(error.value)


def test_missing_or_tampered_artifact(tmp_path):
    payload = b"synthetic-weights"
    artifact = model_artifacts.Artifact("test-release", hashlib.sha256(payload).hexdigest())
    path = tmp_path / "test.weights"
    with pytest.raises(model_artifacts.ModelIntegrityError, match="MODEL_FILE_MISSING"):
        model_artifacts.verify_file(path, artifact)
    path.write_bytes(payload)
    model_artifacts.verify_file(path, artifact)
    path.write_bytes(payload + b"!")
    with pytest.raises(model_artifacts.ModelIntegrityError, match="MODEL_HASH_MISMATCH"):
        model_artifacts.verify_file(path, artifact)


def test_staging_missing_model_is_live_but_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(model_artifacts, "verify_packages", lambda: None)
    settings = staging_settings(
        ocr_backend="easyocr", card_yolo_model_path=str(tmp_path / "missing.pt")
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["code"] == "MODEL_FILE_MISSING"
        response = client.post(
            "/ekyc/documents/cccd_front/extract",
            headers={"X-API-Key": STAGING_KEY},
        )
        assert response.status_code == 503
        assert "MODEL_UNAVAILABLE" in response.text


def test_staging_metrics_authentication_and_server_request_id():
    with TestClient(create_app(staging_settings())) as client:
        assert client.get("/ekyc/metrics").status_code == 401
        marker = "synthetic-private-request-id"
        response = client.get("/health", headers={"X-Request-ID": marker})
        assert response.headers["X-Request-ID"] != marker
        assert UUID(hex=response.headers["X-Request-ID"]).version == 4
        response = client.get("/ekyc/metrics", headers={"X-API-Key": STAGING_KEY})
        assert response.status_code == 200
        assert "ekyc_inference_queue_depth 0" in response.text
        assert marker not in response.text
        assert STAGING_KEY not in response.text


def test_metrics_report_business_errors_and_bounded_latency():
    metrics = RuntimeMetrics()
    for _ in range(2050):
        metrics.request(1, 200, ErrorCode.CARD_NOT_FOUND)
    metrics.queue(waiting=1)
    metrics.queue(waiting=-1, active=1)
    metrics.queue(active=-1)
    metrics.outcome("timeout", 1, 42)
    metrics.restart()
    result = metrics.render()
    assert len(metrics.latency["request"]) == 2048
    assert 'status="business_error"} 2050' in result
    assert 'stage="preparation",code="CARD_NOT_FOUND"} 2050' in result
    assert 'kind="request",quantile="0.99"} 1.000000' in result
    assert 'outcome="restart"} 1' in result
    assert "ekyc_inference_worker_peak_ram_mib 42" in result

@pytest.mark.parametrize("environment", ["staging", "prod"])
def test_deployed_face_comparison_accepts_explicit_opt_in(environment, tmp_path):
    from ekyc.config import Settings
    from ekyc.services.face_comparison import enabled

    manifest = tmp_path / "manifest.json"
    manifest.touch()
    settings = Settings(
        _env_file=None,
        env=environment,
        ekyc_api_key="synthetic-test-key-for-face-feature",
        ocr_backend="easyocr",
        face_comparison_enabled=True,
        face_comparison_manifest=manifest,
    )
    assert enabled(settings)
