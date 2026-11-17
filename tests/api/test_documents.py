from ekyc.deps import get_cccd_service
from ekyc.schemas.document import ExtractionResult

PATH = "/ekyc/documents/gplx_back/extract"
HEADERS = {"X-API-Key": "test-key", "Content-Type": "image/png"}


def test_extraction_endpoint_contract(app, client):
    class FakeWorker:
        def extract(self, data, slot):
            assert data == b"synthetic bytes"
            assert slot == "gplx_back"
            return ExtractionResult(slot=slot, ocr_status="OCR_UNSUPPORTED")

    app.dependency_overrides[get_cccd_service] = FakeWorker
    assert client.post(PATH, content=b"synthetic bytes").status_code == 401
    response = client.post(PATH, content=b"synthetic bytes", headers=HEADERS)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"]["ocrStatus"] == "OCR_UNSUPPORTED"

    app.state.settings = app.state.settings.model_copy(update={"max_image_bytes": 8})
    response = client.post(PATH, content=b"synthetic bytes", headers=HEADERS)
    assert response.json()["errorCode"] == "IMAGE_TOO_LARGE"
