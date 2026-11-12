import logging
import secrets
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ekyc.services.cccd_service import CccdService

from fastapi import Header, Request

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode


def verify_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    settings: Settings = request.app.state.settings
    expected = settings.ekyc_api_key.get_secret_value().encode()
    if x_api_key is None or not secrets.compare_digest(x_api_key.encode(), expected):
        raise EkycError(ErrorCode.UNAUTHORIZED)


def get_cccd_service(request: Request) -> "CccdService":
    from ekyc.services.cccd_service import CccdService

    state = request.app.state
    if state.ocr_engine is None:
        logging.getLogger(__name__).error("ocr_engine_unavailable")
        raise EkycError(ErrorCode.INTERNAL_ERROR)
    return CccdService(
        state.settings, state.card_preparer, state.ocr_engine, state.doc_classifier, state.job_slots
    )
