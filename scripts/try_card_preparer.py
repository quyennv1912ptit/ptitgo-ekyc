"""Inspect one local card crop without running OCR; files contain private data."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.pipeline.card_detector import detect_and_warp_card
from ekyc.pipeline.card_image import DefaultCardPreparer, quality_settings
from ekyc.pipeline.image_loader import load_image_from_bytes
from ekyc.pipeline.quality import inspect_image_quality


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--output", type=Path, default=Path("tests/fixtures/private/card_preview"))
    parser.add_argument("--profile", choices=["strict", "evaluation"])
    args = parser.parse_args()
    settings = Settings(ekyc_api_key="local-preview")
    if args.profile:
        settings = Settings(
            **{
                **settings.model_dump(),
                "quality_profile": args.profile,
            }
        )
    preparer = DefaultCardPreparer(settings)
    image = load_image_from_bytes(args.image.read_bytes(), settings).bgr
    detection = (
        preparer.detector.detect(image)
        if preparer.detector is not None
        else detect_and_warp_card(image)
    )
    metrics, errors = inspect_image_quality(detection.warped_image, quality_settings(settings))
    args.output.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(args.output / "crop.jpg"), detection.warped_image)
    cv2.polylines(image, [detection.corners.astype(np.int32)], True, (0, 0, 255), 5)
    cv2.imwrite(str(args.output / "overlay.jpg"), image)
    report = {"quality": asdict(metrics), "errors": [e.value for e in errors]}
    (args.output / "quality.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
