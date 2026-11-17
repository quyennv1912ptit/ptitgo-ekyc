import base64
import binascii
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.deps import get_cccd_service
from ekyc.schemas.common import ApiResponse
from ekyc.services.face_comparison import FaceResult, enabled

FaceSlot = Literal["cccd_front", "gplx_front", "selfie"]

class BoundedFaceRequestRoute(APIRoute):
    def get_route_handler(self):
        handle = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            max_image_bytes = request.app.state.settings.max_image_bytes
            # Three base64 images plus the JSON envelope.
            limit = 3 * (max_image_bytes * 4 // 3 + 64) + 4096
            received = 0

            async def receive():
                nonlocal received
                message = await request.receive()
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(status_code=413)
                return message

            return await handle(Request(request.scope, receive=receive))

        return bounded


router = APIRouter(route_class=BoundedFaceRequestRoute)


class FaceEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quality_only: bool = True
    images: dict[FaceSlot, str] = Field(min_length=1, max_length=3)


def _decode_images(raw_images: dict[FaceSlot, str], max_bytes: int) -> dict[str, bytes]:
    decoded: dict[str, bytes] = {}
    for slot, encoded in raw_images.items():
        if not encoded or len(encoded) > (max_bytes * 4 // 3) + 64:
            raise EkycError(ErrorCode.IMAGE_TOO_LARGE)
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise EkycError(ErrorCode.INVALID_IMAGE) from exc
        if not raw:
            raise EkycError(ErrorCode.INVALID_IMAGE)
        if len(raw) > max_bytes:
            raise EkycError(ErrorCode.IMAGE_TOO_LARGE)
        decoded[slot] = raw
    return decoded


@router.post(
    "/face/evaluate",
    response_model=ApiResponse[FaceResult],
    response_model_exclude_none=True,
)
async def evaluate_face(
    payload: FaceEvaluateRequest,
    request: Request,
    response: Response,
) -> ApiResponse[FaceResult]:
    settings = request.app.state.settings
    decoded = _decode_images(payload.images, settings.max_image_bytes)
    response.headers["Cache-Control"] = "no-store"

    if not enabled(settings):
        return ApiResponse.ok(FaceResult(reason="DISABLED"), request.state.request_id)

    override = request.app.dependency_overrides.get(get_cccd_service)
    worker = override() if override is not None else get_cccd_service(request)
    operation = worker.check_face_quality if payload.quality_only else worker.compare_faces
    result = await run_in_threadpool(operation, decoded)
    return ApiResponse.ok(result, request.state.request_id)
