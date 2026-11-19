"""Supervised process for serial, persistent document inference."""

import logging
import multiprocessing as mp
from collections.abc import Callable
from functools import cache, partial
from importlib import import_module
from multiprocessing.connection import Connection
from threading import BoundedSemaphore, Event, Lock, Timer
from time import monotonic
from typing import Literal

import cv2

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.core.inference_metrics import peak_ram_mib, timings
from ekyc.core.logging import setup_logging
from ekyc.core.operations_metrics import metrics as runtime_metrics
from ekyc.pipeline.doc_classifier import DocClassifier
from ekyc.schemas.document import DocumentSlot, ExtractionResult
from ekyc.services.cccd_service import CccdResult, CccdService, check_deadline
from ekyc.services.face_comparison import FaceComparison, FaceResult, OpenCvFaceEngine, enabled
from ekyc.services.model_artifacts import ocr_directory

logger = logging.getLogger(__name__)


def build_service(settings: Settings, *, engine_factory=None) -> CccdService:
    def default_engine(cfg):
        from ekyc.pipeline.ocr.easyocr_engine import EasyOcrEngine

        return EasyOcrEngine(
            cfg.ocr_languages,
            width_ths=cfg.ocr_width_ths,
            download_enabled=False,
            model_storage_directory=str(ocr_directory(cfg)),
        )

    service = CccdService(settings, None, None, DocClassifier(settings), BoundedSemaphore(1))
    service.extraction.engine_factory = engine_factory or default_engine
    if settings.inference_ocr_factory:
        module, separator, attribute = settings.inference_ocr_factory.partition(":")
        if not separator or not module or not attribute:
            raise ValueError("Use module:callable for INFERENCE_OCR_FACTORY")
        service.extraction.engine_factory = getattr(import_module(module), attribute)
    return service


def build_demo_service(settings: Settings) -> CccdService:
    from ekyc.driver_extraction import demo_engine

    return build_service(settings, engine_factory=demo_engine)


def handle_job(
    conn: Connection,
    service: CccdService,
    message: tuple,
    get_face: Callable[[], FaceComparison],
) -> None:
    operation, args, deadline = message
    metrics: dict[str, float] = {}
    token = timings.set(metrics)
    try:
        check_deadline(deadline)
        if operation == "warm":
            service.extraction.warm_up()
            result = None
        elif operation in {"face", "face_quality"}:
            result = FaceResult(reason="DISABLED")
            if enabled(service.settings):
                try:
                    face = get_face()
                except (OSError, ValueError, cv2.error):
                    result = FaceResult(reason="MODEL_UNAVAILABLE")
                else:
                    result = face.evaluate(
                        args[0],
                        partial(check_deadline, deadline),
                        quality_only=operation == "face_quality",
                    )
        elif operation == "cccd":
            result = service.run(*args, deadline=deadline)
        else:
            if len(args) == 4:
                data, slot, source, ocr_mode = args
            else:
                data, slot, source = args
                ocr_mode = "full"

            def publish_partial(result: ExtractionResult) -> None:
                conn.send(("partial", result, metrics, peak_ram_mib()))

            result = service.extraction.extract(
                data,
                slot,
                qr=source != "ocr",
                ocr=source != "qr",
                ocr_mode=ocr_mode,
                check=partial(check_deadline, deadline),
                on_partial=publish_partial,
            )
        response = "ok", result
    except EkycError as exc:
        response = "error", (exc.code, exc.message, exc.details)
    except Exception:
        response = "error", (ErrorCode.INTERNAL_ERROR, None, None)
    finally:
        timings.reset(token)
    conn.send((*response, metrics, peak_ram_mib()))


def serve(conn: Connection, settings: Settings, factory: Callable[[Settings], CccdService]) -> None:
    setup_logging(settings.log_level)
    with conn:
        try:
            try:
                service = factory(settings)
            except Exception:
                conn.send("failed")
                return

            @cache
            def get_face() -> FaceComparison:
                return FaceComparison(
                    settings,
                    service.extraction.prepare_native_card,
                    OpenCvFaceEngine(settings.face_comparison_manifest),
                )

            conn.send("ready")
            while True:
                # No reference to the previous image or result while waiting.
                handle_job(conn, service, conn.recv(), get_face)
        except (EOFError, OSError):
            return


def kill_process(process) -> None:
    try:
        if process.is_alive():
            process.kill()
    except ProcessLookupError:
        pass


class CccdWorker:
    """One active job plus a bounded set of admitted waiters per API process."""

    def __init__(
        self, settings: Settings, factory: Callable[[Settings], CccdService] = build_service
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.context = mp.get_context("spawn")
        self.slots = BoundedSemaphore(settings.max_concurrent_jobs)
        self.lock = Lock()
        self.closed = Event()
        self.process = None
        self.conn = None
        self.starts = 0
        self.warmed = False

    def _stop(self) -> None:
        self.warmed = False
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        if self.process is not None:
            kill_process(self.process)
            self.process.join()
            self.process.close()
            self.process = None

    def close(self) -> None:
        self.closed.set()
        with self.lock:
            self._stop()

    def readiness(self) -> tuple[bool, str]:
        if self.closed.is_set():
            return False, "stopping"
        # Readiness describes admission. Lazy model health is established on first inference.
        if not self.lock.acquire(blocking=False):
            return True, "busy"
        try:
            if self.process is not None and self.process.is_alive():
                state = "warm" if self.warmed else "started"
            else:
                state = "recoverable" if self.starts else "lazy"
            return True, state
        finally:
            self.lock.release()

    def _start(self) -> None:
        parent, child = self.context.Pipe()
        process = self.context.Process(
            target=serve, args=(child, self.settings, self.factory), daemon=True
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            process.close()
            raise
        finally:
            child.close()
        self.process, self.conn = process, parent
        self.starts += 1
        if self.starts > 1:
            runtime_metrics.restart()
            logger.info("inference_worker_restart count=%d", self.starts - 1)

    def run(self, front_url: str, back_url: str) -> CccdResult:
        return self._run("cccd", (front_url, back_url))

    def extract(
        self,
        data: bytes,
        slot: DocumentSlot,
        source: Literal["both", "qr", "ocr"] = "both",
        *,
        ocr_mode: Literal["full", "classify_only"] = "full",
        timeout: float | None = None,
    ) -> ExtractionResult:
        payload = (data, slot, source) if ocr_mode == "full" else (data, slot, source, ocr_mode)
        return self._run("extract", payload, timeout)

    def check_face_quality(self, images: dict[str, bytes]) -> FaceResult:
        return self._run("face_quality", (images,))

    def compare_faces(self, images: dict[str, bytes]) -> FaceResult:
        return self._run("face", (images,))

    def warm_up(self) -> None:
        self._run("warm", ())

    def _receive(self, deadline: float):
        while True:
            if self.closed.is_set():
                raise EkycError(ErrorCode.ENGINE_BUSY)
            check_deadline(deadline)
            if self.conn.poll(min(0.05, max(0, deadline - monotonic()))):
                return self.conn.recv()

    def _exchange(self, operation, args, deadline, metrics, on_partial):
        if self.process is None or not self.process.is_alive():
            self._stop()
            started = monotonic()
            self._start()
            if self._receive(deadline) != "ready":
                raise EkycError(ErrorCode.INTERNAL_ERROR)
            metrics["worker_start_ms"] = (monotonic() - started) * 1000
        check_deadline(deadline)
        # The watchdog also covers a blocked pipe write while sending image bytes.
        timer = Timer(max(0, deadline - monotonic()), kill_process, (self.process,))
        timer.daemon = True
        timer.start()
        try:
            self.conn.send((operation, args, deadline))
            while True:
                response = self._receive(deadline)
                if response[0] != "partial":
                    return response
                on_partial(*response[1:])
        except (EOFError, OSError):
            check_deadline(deadline)
            raise
        finally:
            timer.cancel()
            timer.join()

    def _run(self, operation, args, timeout=None):
        started = monotonic()
        deadline = started + (self.settings.cccd_timeout_s if timeout is None else timeout)
        metrics = {}
        peak = None
        outcome = "ok"
        partial_result = None

        def save_partial(result, child_metrics, child_peak):
            nonlocal partial_result, peak
            partial_result = result
            metrics.update(child_metrics)
            peak = child_peak

        try:
            if self.closed.is_set() or not self.slots.acquire(blocking=False):
                raise EkycError(ErrorCode.ENGINE_BUSY)
            runtime_metrics.queue(waiting=1)
            queued = True
            try:
                wait = min(self.settings.job_slot_timeout_s, max(0, deadline - monotonic()))
                acquired = self.lock.acquire(timeout=wait)
                metrics["queue_ms"] = (monotonic() - started) * 1000
                if not acquired:
                    raise EkycError(ErrorCode.ENGINE_BUSY)
                runtime_metrics.queue(waiting=-1, active=1)
                queued = False
                try:
                    if self.closed.is_set():
                        raise EkycError(ErrorCode.ENGINE_BUSY)
                    status, payload, child_metrics, peak = self._exchange(
                        operation, args, deadline, metrics, save_partial
                    )
                    metrics.update(child_metrics)
                    check_deadline(deadline)
                    if status == "error":
                        code, message, details = payload
                        raise EkycError(code, message, details=details)
                    if operation == "warm":
                        self.warmed = True
                    return payload
                except EkycError as exc:
                    if self.closed.is_set() or exc.code in (
                        ErrorCode.ENGINE_TIMEOUT,
                        ErrorCode.INTERNAL_ERROR,
                    ):
                        self._stop()
                    if exc.code == ErrorCode.ENGINE_TIMEOUT and partial_result is not None:
                        outcome = exc.code.value
                        return partial_result
                    raise
                except (EOFError, OSError):
                    self._stop()
                    check_deadline(deadline)
                    raise EkycError(ErrorCode.INTERNAL_ERROR) from None
                finally:
                    runtime_metrics.queue(active=-1)
                    self.lock.release()
            finally:
                if queued:
                    runtime_metrics.queue(waiting=-1)
                self.slots.release()
        except EkycError as exc:
            outcome = exc.code.value
            raise
        finally:
            elapsed = monotonic() - started
            runtime_metrics.outcome(outcome, elapsed, peak)
            logger.info(
                "inference operation=%s outcome=%s total_ms=%.3f stage_ms=%s peak_ram_mib=%s",
                operation,
                outcome,
                elapsed * 1000,
                {key: round(value, 3) for key, value in metrics.items()},
                round(peak, 3) if peak is not None else None,
            )
