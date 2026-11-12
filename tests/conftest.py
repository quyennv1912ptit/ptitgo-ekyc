from collections.abc import Iterator

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import Field

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.schemas.common import ApiResponse, CamelModel


class SampleBody(CamelModel):
    count: int = Field(gt=0)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, ekyc_api_key="test-key", ocr_backend="none")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    from ekyc.api.router import ekyc_router
    from ekyc.main import create_app

    app = create_app(settings)

    @app.get("/test/error/{code}")
    def fail(code: ErrorCode) -> None:
        raise EkycError(code)

    @app.get("/test/crash")
    def crash() -> None:
        raise RuntimeError("secret")

    @app.post(
        "/test/body", response_model=ApiResponse[SampleBody], response_model_exclude_none=True
    )
    def body(value: SampleBody, request: Request) -> ApiResponse[SampleBody]:
        return ApiResponse.ok(value, request.state.request_id)

    # Chỉ gắn route kiểm thử trên bản sao router, không sửa router sản phẩm.
    protected = APIRouter(prefix=ekyc_router.prefix, dependencies=ekyc_router.dependencies)

    @protected.get("/test", response_model=ApiResponse[dict], response_model_exclude_none=True)
    def authorized(request: Request) -> ApiResponse[dict]:
        return ApiResponse.ok({}, request.state.request_id)

    app.include_router(protected)
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client
