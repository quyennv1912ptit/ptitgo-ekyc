import logging
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ekyc.services.cccd_worker import CccdWorker

from fastapi import Header, Request

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode


def verify_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.ekyc_api_key.get_secret_value().encode()
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected):
        raise EkycError(ErrorCode.UNAUTHORIZED)


def get_cccd_service(request: Request) -> "CccdWorker":
    worker = request.app.state.cccd_worker
    if worker is None:
        if getattr(request.app.state, "inference_readiness_error", None):
            raise EkycError(ErrorCode.MODEL_UNAVAILABLE)
        logging.getLogger(__name__).error("ocr_engine_unavailable")
        raise EkycError(ErrorCode.INTERNAL_ERROR)
    return worker
