from fastapi import APIRouter, Depends, Response

from ekyc.api.routes.cccd import router as cccd_router
from ekyc.api.routes.documents import router as document_router
from ekyc.api.routes.driver_verification import router as driver_router
from ekyc.api.routes.face import router as face_router
from ekyc.api.routes.health import router as health_router
from ekyc.core.operations_metrics import metrics
from ekyc.deps import verify_api_key

api_router = APIRouter()
ekyc_router = APIRouter(prefix="/ekyc", dependencies=[Depends(verify_api_key)])
ekyc_router.include_router(cccd_router)
ekyc_router.include_router(document_router)
ekyc_router.include_router(face_router)
api_router.include_router(health_router)


@ekyc_router.get("/metrics", include_in_schema=False)
def inference_metrics() -> Response:
    return Response(
        metrics.render(),
        media_type="text/plain; version=0.0.4",
        headers={"Cache-Control": "no-store"},
    )


api_router.include_router(ekyc_router)
api_router.include_router(driver_router)
