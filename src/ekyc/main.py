import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware

from ekyc.api.router import api_router
from ekyc.config import Settings, get_settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.core.handlers import error_response, register_exception_handlers
from ekyc.core.logging import setup_logging
from ekyc.core.middleware import RequestIdMiddleware
from ekyc.core.operations_metrics import MetricsMiddleware
from ekyc.services.cccd_worker import CccdWorker
from ekyc.services.driver_auth import FirebaseVerifier
from ekyc.services.model_artifacts import ModelIntegrityError, verify_models
from ekyc.services.verification_store import VerificationStore

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.verification_uploads = BoundedSemaphore(settings.verification_parallel_uploads)
        app.state.verification_auth = None
        app.state.verification_store = None
        app.state.cccd_worker = None
        app.state.inference_readiness_error = None
        app.state.face_engine = None  # The experimental model lives in the worker process.
        app.state.face_jobs = BoundedSemaphore(1)
        logger.info("startup")
        try:
            if bool(settings.firebase_project_id) != bool(settings.verification_db_path):
                raise RuntimeError("Configure both Firebase project and verification database")
            if settings.firebase_project_id and settings.verification_db_path:
                app.state.verification_store = await run_in_threadpool(
                    VerificationStore.from_settings, settings
                )
                app.state.verification_auth = FirebaseVerifier(settings.firebase_project_id)
            if settings.ocr_backend == "easyocr":
                try:
                    if settings.deployed:
                        await run_in_threadpool(verify_models, settings)
                    app.state.cccd_worker = CccdWorker(settings)
                    if settings.inference_warm_up:
                        await run_in_threadpool(app.state.cccd_worker.warm_up)
                except (ModelIntegrityError, EkycError) as exc:
                    if not settings.deployed:
                        raise
                    reason = (
                        exc.code if isinstance(exc, ModelIntegrityError) else "MODEL_WARMUP_FAILED"
                    )
                    app.state.inference_readiness_error = reason
                    logger.error("inference_not_ready code=%s", reason)
                    if app.state.cccd_worker is not None:
                        await run_in_threadpool(app.state.cccd_worker.close)
                        app.state.cccd_worker = None
            yield
        finally:
            if app.state.cccd_worker is not None:
                await run_in_threadpool(app.state.cccd_worker.close)
            if app.state.verification_auth is not None:
                app.state.verification_auth.close()
            logger.info("shutdown")

    production = settings.env == "prod"
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )

    @app.middleware("http")
    async def verification_privacy(request, call_next):
        verification = request.url.path.startswith("/ekyc/driver-verification/")
        if verification and settings.deployed and request.url.scheme != "https":
            return error_response(request, EkycError(ErrorCode.FORBIDDEN))
        try:
            response = await call_next(request)
        except Exception:
            if not settings.deployed:
                raise
            logger.exception("unhandled_error")
            response = error_response(request, EkycError(ErrorCode.INTERNAL_ERROR))
        if verification:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.add_middleware(RequestIdMiddleware, trust_client_ids=not settings.deployed)
    app.add_middleware(MetricsMiddleware)
    if settings.verification_admin_origins:
        for origin in settings.verification_admin_origins:
            url = urlsplit(origin)
            valid_scheme = url.scheme == "https" or (
                not production
                and url.scheme == "http"
                and url.hostname in {"localhost", "127.0.0.1", "::1"}
            )
            if (
                not valid_scheme
                or not url.hostname
                or url.username
                or url.password
                or origin != f"{url.scheme}://{url.netloc}"
                or "*" in origin
            ):
                raise ValueError("Configure exact verification admin origins")
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.verification_admin_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
