"""Measure Phase 10.1 one-shot demo against its persistent worker on a synthetic card."""

import json
import logging
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from time import monotonic

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import EkycError
from ekyc.services.cccd_worker import CccdWorker, build_demo_service


def image_bytes() -> bytes:
    image = np.full((680, 1060, 3), 170, dtype=np.uint8)
    cv2.rectangle(image, (20, 20), (1040, 660), (235, 235, 235), -1)
    cv2.putText(image, "SYNTHETIC DOCUMENT", (150, 200), 0, 1.6, (20, 20, 20), 3)
    cv2.putText(image, "SAMPLE FIXTURE", (150, 350), 0, 1.6, (20, 20, 20), 3)
    _, buffer = cv2.imencode(".png", image)
    return buffer.tobytes()


def one_shot(raw: bytes) -> None:
    for source, timeout in (("qr", 8), ("ocr", 35)):
        process = subprocess.run(
            [sys.executable, "-m", "ekyc.driver_extraction", source, "cccd_front"],
            input=raw,
            capture_output=True,
            timeout=timeout,
            check=True,
        )
        if json.loads(process.stdout.splitlines()[-1])["status"] == "OCR_FAILED":
            raise RuntimeError("Baseline OCR failed")


def persistent(raw: bytes, worker: CccdWorker) -> None:
    worker.extract(raw, "cccd_front", "qr", timeout=8)
    if worker.extract(raw, "cccd_front", "ocr", timeout=35).ocr_status == "OCR_FAILED":
        raise RuntimeError("Persistent OCR failed")


def sample(call):
    started = monotonic()
    try:
        call()
        status = "ok"
    except EkycError as exc:
        status = exc.code.value
    return status, round((monotonic() - started) * 1000, 1)


def main():
    logging.basicConfig(level=logging.INFO)
    raw = image_bytes()
    config = Settings(_env_file=None, ekyc_api_key="benchmark-synthetic")
    worker = CccdWorker(config, build_demo_service)
    try:
        for name, call in (
            ("one_shot", lambda: one_shot(raw)),
            ("persistent", lambda: persistent(raw, worker)),
        ):
            cold = sample(call)
            sequential = [sample(call) for _ in range(3)]
            with ThreadPoolExecutor(3) as pool:
                concurrent = list(pool.map(lambda _, current=call: sample(current), range(3)))
            print(
                json.dumps(
                    {
                        "mode": name,
                        "cold_ms": cold,
                        "sequential": sequential,
                        "sequential_median_ms": statistics.median(t for _, t in sequential),
                        "concurrent": concurrent,
                    }
                ),
                flush=True,
            )
    finally:
        worker.close()


if __name__ == "__main__":
    main()
