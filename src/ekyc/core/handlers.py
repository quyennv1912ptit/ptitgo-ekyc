import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.core.logging import request_id_ctx
from ekyc.schemas.common import ApiResponse

logger = logging.getLogger(__name__)


def error_response(request: Request, error: EkycError, status: int | None = None) -> JSONResponse:
    identifier = getattr(request.state, "request_id", "-")
    headers = {"X-Request-ID": identifier}
    if error.code == ErrorCode.ENGINE_BUSY:
        headers["Retry-After"] = "1"
    return JSONResponse(
        status_code=status if status is not None else error.http_status,
        content=ApiResponse.fail(error, identifier).model_dump(
            mode="json", by_alias=True, exclude_none=True
        ),
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(EkycError)
    async def handle_ekyc(request: Request, exc: EkycError) -> JSONResponse:
        return error_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        details = [{"loc": list(error["loc"]), "msg": error["msg"]} for error in exc.errors()]
        return error_response(request, EkycError(ErrorCode.INVALID_REQUEST, details=details))

    @app.exception_handler(StarletteHTTPException)
    async def handle_http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: ErrorCode.NOT_FOUND, 405: ErrorCode.METHOD_NOT_ALLOWED}.get(
            exc.status_code, ErrorCode.INVALID_REQUEST
        )
        return error_response(request, EkycError(code), exc.status_code)

    @app.exception_handler(Exception)
    async def handle_unknown(request: Request, exc: Exception) -> JSONResponse:
        # Handler 500 chạy ngoài middleware; khôi phục context để log đúng request.
        token = request_id_ctx.set(getattr(request.state, "request_id", "-"))
        try:
            logger.error("unhandled_error", exc_info=(type(exc), exc, exc.__traceback__))
        finally:
            request_id_ctx.reset(token)
        return error_response(request, EkycError(ErrorCode.INTERNAL_ERROR))
