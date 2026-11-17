from typing import Annotated

from fastapi import APIRouter, Depends, Request

from ekyc.deps import get_cccd_service
from ekyc.schemas.cccd import CccdData, CccdRequest
from ekyc.schemas.common import ApiResponse
from ekyc.services.cccd_worker import CccdWorker

router = APIRouter()


@router.post(
    "/cccd",
    response_model=ApiResponse[CccdData],
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
def verify_cccd(
    body: CccdRequest, request: Request, service: Annotated[CccdWorker, Depends(get_cccd_service)]
) -> ApiResponse[CccdData]:
    result = service.run(body.front_url, body.back_url)
    return ApiResponse.ok(result.data, request.state.request_id, result.warnings)
