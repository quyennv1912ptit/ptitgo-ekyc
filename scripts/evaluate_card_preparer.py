"""Local detector/quality evaluation; outputs include private card images. No OCR."""

import argparse
import hashlib
import json
import os
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import EkycError
from ekyc.pipeline.card_detector import detect_and_warp_card, order_points
from ekyc.pipeline.card_image import DefaultCardPreparer, quality_settings
from ekyc.pipeline.image_loader import load_image_from_bytes
from ekyc.pipeline.quality import inspect_image_quality


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="JSON rows: id, path; optional corners/split")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=["yolo", "opencv"], default="yolo")
    parser.add_argument("--model", default="models/card_yolov8n_pose.experimental.pt")
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    os.environ["YOLO_CONFIG_DIR"] = str(Path("models/ultralytics_settings").resolve())
    os.environ["YOLO_OFFLINE"] = "true"
    import torch

    torch.set_num_threads(args.threads)
    settings = Settings(
        _env_file=None,
        ekyc_api_key="local-evaluation",
        ocr_backend="none",
        card_detector_backend=args.backend,
        card_yolo_model_path=args.model,
    )
    preparer = DefaultCardPreparer(settings)
    if preparer.detector is not None:
        preparer.detector.model.add_callback(
            "on_predict_start", lambda predictor: torch.set_num_threads(args.threads)
        )
    profiles = {
        name: quality_settings(settings.model_copy(update={"quality_profile": name}))
        for name in ("strict", "evaluation")
    }
    args.output.mkdir(parents=True, exist_ok=True)
    rows = json.loads(args.manifest.read_text(encoding="utf-8"))
    results = []
    for row in rows:
        identifier = row["id"]
        if not identifier.isalnum():
            raise ValueError("Unsafe image ID")
        started = perf_counter()
        result = {
            "id": identifier,
            "split": row.get("split") if row.get("include", True) else "excluded",
            "detector": "ok",
        }
        raw = Path(row["path"]).read_bytes()
        result["sha256"] = hashlib.sha256(raw).hexdigest()
        try:
            image = load_image_from_bytes(raw, settings).bgr
            detected = (
                preparer.detector.detect(image)
                if preparer.detector is not None
                else detect_and_warp_card(image)
            )
            result["corners"] = detected.corners.tolist()
            result["native_shape"] = list(detected.warped_image.shape)
            result["quality"] = {}
            for name, profile in profiles.items():
                metrics, errors = inspect_image_quality(detected.warped_image, profile)
                result["quality"][name] = {
                    "metrics": asdict(metrics),
                    "errors": [error.value for error in errors],
                }
            if row.get("corners") and row.get("include", True):
                reference = order_points(
                    np.array(row["corners"], np.float32) * [image.shape[1], image.shape[0]]
                )
                # Geometric correspondence is cyclic near 45 degrees; use best cyclic alignment.
                distance = min(
                    np.linalg.norm(
                        np.roll(reference, shift, axis=0) - detected.corners, axis=1
                    ).mean()
                    for shift in range(4)
                )
                result["corner_error_image_diagonal"] = float(distance / np.hypot(*image.shape[:2]))
            crop = cv2.resize(detected.warped_image, (1600, 1009))
            cv2.imwrite(str(args.output / f"{identifier}_crop.jpg"), crop)
            overlay = image.copy()
            cv2.polylines(overlay, [detected.corners.astype(np.int32)], True, (0, 0, 255), 5)
            factor = min(1, 1200 / max(overlay.shape[:2]))
            cv2.imwrite(
                str(args.output / f"{identifier}_overlay.jpg"),
                cv2.resize(overlay, None, fx=factor, fy=factor),
            )
        except EkycError as error:
            result["detector"] = error.code.value
        result["seconds"] = round(perf_counter() - started, 3)
        results.append(result)
        print(identifier, result["detector"], flush=True)
    summary = {
        "total": len(results),
        "backend": args.backend,
        "detector": dict(Counter(r["detector"] for r in results)),
        "quality_pass": {
            name: sum(not r["quality"][name]["errors"] for r in results if "quality" in r)
            for name in profiles
        },
        "checkpoint_sha256": hashlib.sha256(Path(args.model).read_bytes()).hexdigest()
        if args.backend == "yolo"
        else None,
        "settings": {
            key: value
            for key, value in settings.model_dump(mode="json").items()
            if key.startswith(("card_", "evaluation_"))
        },
        "note": "Training images included; approximate manual labels; no OCR accuracy measured.",
    }
    (args.output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("total", "detector", "quality_pass")}))


if __name__ == "__main__":
    main()
