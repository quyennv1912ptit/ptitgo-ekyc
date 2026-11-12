import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekyc.config import Settings


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    "code,status,retryable",
    [
        ("IMAGE_BLURRY", 200, False),
        ("IMAGE_FETCH_FAILED", 502, True),
    ],
)
def test_errors(client: TestClient, code: str, status: int, retryable: bool) -> None:
    response = client.get(f"/test/error/{code}")
    assert response.status_code == status
    body = response.json()
    assert body["success"] is False
    assert body["errorCode"] == code
    assert body["retryable"] is retryable
    assert body["warnings"] == []
    assert "data" not in body


def test_private_exception(client: TestClient) -> None:
    response = client.get("/test/crash", headers={"X-Request-ID": "crash-id"})
    assert response.status_code == 500
    assert response.json()["errorCode"] == "INTERNAL_ERROR"
    assert "secret" not in response.text
    assert "Traceback" not in response.text
    assert response.headers["X-Request-ID"] == response.json()["requestId"] == "crash-id"


def test_validation(client: TestClient) -> None:
    response = client.post("/test/body", json={"count": "signed-url-secret"})
    assert response.status_code == 400
    assert response.json()["errorCode"] == "INVALID_REQUEST"
    assert "signed-url-secret" not in response.text
    assert all(set(detail) == {"loc", "msg"} for detail in response.json()["details"])


@pytest.mark.parametrize(
    "method,path,status,code",
    [
        ("get", "/absent", 404, "NOT_FOUND"),
        ("post", "/health", 405, "METHOD_NOT_ALLOWED"),
    ],
)
def test_http_errors(client: TestClient, method: str, path: str, status: int, code: str) -> None:
    response = client.request(method, path)
    assert response.status_code == status
    assert response.json()["errorCode"] == code
    assert response.json()["success"] is False


@pytest.mark.parametrize("key", [None, "wrong", "test-key"])
def test_auth(client: TestClient, key: str | None) -> None:
    response = client.get("/ekyc/test", headers={"X-API-Key": key} if key else {})
    assert response.status_code == (200 if key == "test-key" else 401)
    if key != "test-key":
        assert response.json()["errorCode"] == "UNAUTHORIZED"
    else:
        assert response.json()["success"] is True


@pytest.mark.parametrize(
    "header,expected",
    [("client-123_ABC", "client-123_ABC"), ("a! b@c", "abc"), ("a" * 80, "a" * 64)],
)
def test_request_id(client: TestClient, header: str, expected: str) -> None:
    response = client.get("/test/error/IMAGE_BLURRY", headers={"X-Request-ID": header})
    assert response.headers["X-Request-ID"] == response.json()["requestId"] == expected


def test_generated_request_id(client: TestClient) -> None:
    response = client.get("/test/error/IMAGE_BLURRY")
    assert re.fullmatch("[0-9a-f]{32}", response.json()["requestId"])
    assert response.headers["X-Request-ID"] == response.json()["requestId"]


def test_success_envelope(client: TestClient) -> None:
    response = client.post("/test/body", json={"count": 1})
    assert response.json() == {
        "success": True,
        "data": {"count": 1},
        "warnings": [],
        "requestId": response.headers["X-Request-ID"],
    }


def test_lifespan(app: FastAPI, client: TestClient, settings: Settings) -> None:
    assert app.state.settings is settings
    from ekyc.pipeline.doc_classifier import DocClassifier

    assert isinstance(app.state.doc_classifier, DocClassifier)
    assert app.state.ocr_engine is None
    assert app.state.face_engine is None
    assert app.state.job_slots.acquire(blocking=False)
    app.state.job_slots.release()


def test_prod_docs(settings: Settings) -> None:
    from ekyc.main import create_app

    with TestClient(create_app(settings.model_copy(update={"env": "prod"}))) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404
