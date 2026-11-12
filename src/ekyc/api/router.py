from fastapi import APIRouter, Depends

from ekyc.api.routes.cccd import router as cccd_router
from ekyc.api.routes.health import router as health_router
from ekyc.deps import verify_api_key

api_router = APIRouter()
ekyc_router = APIRouter(prefix="/ekyc", dependencies=[Depends(verify_api_key)])
ekyc_router.include_router(cccd_router)
api_router.include_router(health_router)
api_router.include_router(ekyc_router)
