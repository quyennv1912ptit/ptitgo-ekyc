from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from starlette.concurrency import run_in_threadpool

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.deps import get_cccd_service
from ekyc.schemas.common import ApiResponse
from ekyc.schemas.document import DocumentSlot, ExtractionResult
from ekyc.services.cccd_worker import CccdWorker

router = APIRouter()


@router.post(
    "/documents/{slot}/extract",
    response_model=ApiResponse[ExtractionResult],
    response_model_exclude_none=True,
)
async def extract_document(
    slot: DocumentSlot,
    request: Request,
    response: Response,
    worker: Annotated[CccdWorker, Depends(get_cccd_service)],
) -> ApiResponse[ExtractionResult]:
    limit = request.app.state.settings.max_image_bytes
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise EkycError(ErrorCode.IMAGE_TOO_LARGE)
        body.extend(chunk)
    result = await run_in_threadpool(worker.extract, bytes(body), slot)
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse.ok(result, request.state.request_id)
