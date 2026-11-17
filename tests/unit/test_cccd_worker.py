import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.doc_classifier import DocClassifier
from ekyc.services.cccd_service import CccdService
from ekyc.services.cccd_worker import CccdWorker


class FakePreparer:
    def prepare_loaded(self, loaded):
        return SimpleNamespace(image=loaded.bgr, warnings=[])

    def prepare(self, url):
        if url == "crash":
            os._exit(3)
        if url == "invalid":
            raise EkycError(ErrorCode.INVALID_IMAGE)
        return SimpleNamespace(image=np.zeros((8, 12, 3), dtype=np.uint8))


class BlockingOcr:
    def __init__(self, started):
        self.started = started

    def read_lines(self, image):
        self.started.set()
        time.sleep(60)


class Factory:
    def __init__(self, started, pid):
        self.started = started
        self.pid = pid

    def __call__(self, settings):
        self.pid.value = multiprocessing.current_process().pid
        return CccdService(
            settings,
            FakePreparer(),
            BlockingOcr(self.started),
            DocClassifier(settings),
            BoundedSemaphore(1),
        )


def test_blocking_ocr_is_killed_and_worker_can_restart(settings):
    context = multiprocessing.get_context("spawn")
    started, child_pid = context.Event(), context.Value("i", 0)
    bounded = settings.model_copy(update={"cccd_timeout_s": 8})
    worker = CccdWorker(bounded, Factory(started, child_pid))
    try:
        before = time.monotonic()
        with pytest.raises(EkycError) as info:
            worker.run("front", "back")
        assert info.value.code == ErrorCode.ENGINE_TIMEOUT
        assert started.is_set()
        assert time.monotonic() - before < 15
        assert worker.process is None and worker.conn is None
        assert all(child.pid != child_pid.value for child in multiprocessing.active_children())
        with pytest.raises(EkycError) as recovered:
            worker.run("invalid", "back")
        assert recovered.value.code == ErrorCode.INVALID_IMAGE
        assert worker.process.is_alive()
        pid = worker.process.pid
        _, png = cv2.imencode(".png", np.zeros((24, 40, 3), np.uint8))
        result = worker.extract(png.tobytes(), "gplx_back")
        assert result.ocr_status == "OCR_UNSUPPORTED"
        assert worker.process.pid == pid
    finally:
        worker.close()
    assert all(child.pid != pid for child in multiprocessing.active_children())


def test_assisted_timeout_returns_completed_qr_and_kills_worker(settings):
    context = multiprocessing.get_context("spawn")
    started, child_pid = context.Event(), context.Value("i", 0)
    worker = CccdWorker(settings, Factory(started, child_pid))
    qr = cv2.QRCodeEncoder_create().encode(
        "000000000001||TEN MAU A|01021990|Nam|SYNTHETIC|01012021"
    )
    qr = cv2.resize(qr, None, fx=5, fy=5, interpolation=cv2.INTER_NEAREST)
    image = cv2.copyMakeBorder(qr, 25, 25, 25, 25, cv2.BORDER_CONSTANT, value=255)
    _, encoded = cv2.imencode(".png", image)
    try:
        result = worker.extract(encoded.tobytes(), "cccd_front", timeout=8)
        assert started.is_set()
        assert result.qr_status == "QR_FOUND_SUPPORTED"
        assert result.ocr_status == "OCR_FAILED"
        assert "OCR_UNAVAILABLE" in result.warnings
        assert any(
            field.field_key == "id_number" and field.source == "QR" for field in result.candidates
        )
        assert worker.process is None and worker.conn is None
        assert all(child.pid != child_pid.value for child in multiprocessing.active_children())
        with pytest.raises(EkycError) as recovered:
            worker.run("invalid", "back")
        assert recovered.value.code == ErrorCode.INVALID_IMAGE
        assert worker.starts == 2
    finally:
        worker.close()


def test_crash_restarts_worker(settings):
    context = multiprocessing.get_context("spawn")
    worker = CccdWorker(settings, Factory(context.Event(), context.Value("i", 0)))
    try:
        assert worker.readiness() == (True, "lazy")
        with pytest.raises(EkycError) as crash:
            worker.run("crash", "back")
        assert crash.value.code == ErrorCode.INTERNAL_ERROR
        assert worker.process is None and worker.conn is None
        assert worker.readiness() == (True, "recoverable")
        with pytest.raises(EkycError) as restarted:
            worker.run("invalid", "back")
        assert restarted.value.code == ErrorCode.INVALID_IMAGE
        assert worker.starts == 2
    finally:
        worker.close()
    assert worker.readiness() == (False, "stopping")


def test_queue_capacity_and_shutdown_interrupt_active_job(settings):
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    pid = context.Value("i", 0)
    config = settings.model_copy(
        update={
            "max_concurrent_jobs": 2,
            "job_slot_timeout_s": 5,
            "cccd_timeout_s": 15,
        }
    )
    worker = CccdWorker(config, Factory(started, pid))
    with ThreadPoolExecutor(max_workers=2) as pool:
        try:
            active = pool.submit(worker.run, "front", "back")
            assert started.wait(8)
            queued = pool.submit(worker.run, "invalid", "back")
            # Wait for both slots to be reserved without depending on thread order.
            stop = time.monotonic() + 2
            while worker.slots.acquire(blocking=False):
                worker.slots.release()
                assert time.monotonic() < stop
                time.sleep(0.01)
            before = time.monotonic()
            with pytest.raises(EkycError) as busy:
                worker.run("overflow", "back")
            assert busy.value.code == ErrorCode.ENGINE_BUSY
            assert time.monotonic() - before < 1
            worker.close()
            for future in (active, queued):
                with pytest.raises(EkycError) as shutdown:
                    future.result(timeout=2)
                assert shutdown.value.code == ErrorCode.ENGINE_BUSY
            assert worker.process is None
            assert all(child.pid != pid.value for child in multiprocessing.active_children())
        finally:
            worker.close()


class SyntheticEngine:
    def read_lines(self, image):
        return []


def make_engine(settings):
    return SyntheticEngine()


def test_configured_engine_is_warmed_once_and_reused(settings, caplog):
    worker = CccdWorker(
        settings.model_copy(
            update={
                "inference_ocr_factory": f"{__name__}:make_engine",
            }
        )
    )
    try:
        with caplog.at_level("INFO", logger="ekyc.services.cccd_worker"):
            worker.warm_up()
            assert worker.readiness() == (True, "warm")
            _, png = cv2.imencode(".png", np.zeros((24, 40, 3), np.uint8))
            for _ in range(2):
                result = worker.extract(png.tobytes(), "cccd_front")
                assert result.ocr_status == "OCR_NO_TEXT"
        assert worker.starts == 1
        assert caplog.text.count("ocr_init_ms") == 1
        for stage in ("decode_ms", "card_prepare_ms", "qr_ms", "ocr_ms", "parser_ms", "queue_ms"):
            assert stage in caplog.text
        assert "peak_ram_mib=" in caplog.text
    finally:
        worker.close()
