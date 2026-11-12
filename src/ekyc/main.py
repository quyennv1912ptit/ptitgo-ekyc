import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from threading import BoundedSemaphore

from fastapi import FastAPI

from ekyc.api.router import api_router
from ekyc.config import Settings, get_settings
from ekyc.core.handlers import register_exception_handlers
from ekyc.core.logging import setup_logging
from ekyc.core.middleware import RequestIdMiddleware
from ekyc.pipeline.card_image import DefaultCardPreparer
from ekyc.pipeline.doc_classifier import DocClassifier

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings if settings is not None else get_settings()
    setup_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.job_slots = BoundedSemaphore(settings.max_concurrent_jobs)
        app.state.doc_classifier = DocClassifier(settings)
        app.state.card_preparer = DefaultCardPreparer(settings)
        if settings.ocr_backend == "easyocr":
            from ekyc.pipeline.ocr.easyocr_engine import EasyOcrEngine

            app.state.ocr_engine = EasyOcrEngine(
                settings.ocr_languages, width_ths=settings.ocr_width_ths
            )
            app.state.ocr_engine.warm_up()
        else:
            app.state.ocr_engine = None
        app.state.face_engine = None
        # TODO bước sau: load model face và warm-up ở đây
        logger.info("startup")
        try:
            yield
        finally:
            logger.info("shutdown")

    production = settings.env == "prod"
    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    app.add_middleware(RequestIdMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
