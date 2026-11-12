from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.deps import get_cccd_service
from ekyc.schemas.cccd import CccdData
from ekyc.services.cccd_service import CccdResult

HEADERS = {"X-API-Key": "test-key"}
BODY = {"frontUrl": "https://example.com/front?sig=a%2Fb", "backUrl": "https://example.com/back"}


class FakeService:
    def run(self, front_url: str, back_url: str) -> CccdResult:
        assert front_url == BODY["frontUrl"]
        assert back_url == BODY["backUrl"]
        return CccdResult(
            CccdData(
                id_number="079200123456",
                full_name="NGUYEN VAN A",
                dob=date(2000, 5, 1),
                no_expiry=True,
                field_confidence={"idNumber": 0.9},
                review_fields=["issueDate"],
                review_required=True,
            ),
            [],
        )


def test_cccd_success(app: FastAPI, client: TestClient) -> None:
    app.dependency_overrides[get_cccd_service] = lambda: FakeService()
    response = client.post("/ekyc/cccd", headers=HEADERS, json=BODY)
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["idNumber"] == "079200123456"
    assert body["data"]["dob"] == "2000-05-01"
    assert body["data"]["fieldConfidence"] == {"idNumber": 0.9}
    assert body["data"]["noExpiry"] is True
    assert body["data"]["reviewFields"] == ["issueDate"]
    assert body["data"]["reviewRequired"] is True
    assert "score" not in response.text and "classificationScore" not in response.text
    assert "expiryDate" not in body["data"]
    assert body["requestId"] == response.headers["X-Request-ID"]


def test_cccd_auth_validation(app: FastAPI, client: TestClient) -> None:
    app.dependency_overrides[get_cccd_service] = lambda: FakeService()
    assert client.post("/ekyc/cccd", json=BODY).status_code == 401
    response = client.post("/ekyc/cccd", headers=HEADERS, json={"frontUrl": "short"})
    assert response.status_code == 400
    assert response.json()["errorCode"] == "INVALID_REQUEST"


def test_cccd_business_error(app: FastAPI, client: TestClient) -> None:
    class ErrorService:
        def run(self, front_url: str, back_url: str) -> CccdResult:
            raise EkycError(
                ErrorCode.FIELD_MISSING, details=[{"image": "front", "fields": ["idNumber"]}]
            )

    app.dependency_overrides[get_cccd_service] = lambda: ErrorService()
    response = client.post("/ekyc/cccd", headers=HEADERS, json=BODY)
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["details"] == [{"image": "front", "fields": ["idNumber"]}]


def test_backend_none(client: TestClient) -> None:
    response = client.post("/ekyc/cccd", headers=HEADERS, json=BODY)
    assert response.status_code == 500
    assert response.json()["errorCode"] == "INTERNAL_ERROR"
