"""Select local checkpoints by validation corner error, rather than box/pose mAP."""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path

import cv2
import numpy as np
import yaml

from ekyc.config import Settings
from ekyc.core.errors import EkycError
from ekyc.pipeline.card_detector import order_points
from ekyc.pipeline.yolo_card_detector import rectify_corners, touches_border


def select_checkpoint(data_path: Path, candidates: list[Path], output: Path) -> dict:
    os.environ.setdefault("YOLO_CONFIG_DIR", str(Path("models/ultralytics_settings").resolve()))
    os.environ.setdefault("YOLO_OFFLINE", "true")
    import torch
    from ultralytics import YOLO

    torch.set_num_threads(4)
    data = yaml.safe_load(data_path.read_text(encoding="utf-8"))
    folder = Path(data["path"]) / data["val"]
    images = sorted(folder.glob("*.jpg"))
    if not images:
        raise ValueError("Expected exported validation JPEG folder")
    scores = []
    config = Settings(_env_file=None, ekyc_api_key="checkpoint-selection", ocr_backend="none")
    for candidate in candidates:
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        model = YOLO(str(candidate))
        model.add_callback("on_predict_start", lambda predictor: torch.set_num_threads(4))
        if model.names != {0: "card"} or model.model.yaml.get("kpt_shape") != [4, 3]:
            raise ValueError("Expected a four-corner card model")
        errors, valid_errors, missing, invalid = [], [], 0, 0
        for path in images:
            image = cv2.imread(str(path))
            h, w = image.shape[:2]
            label_path = folder.parent.parent / "labels" / folder.name / f"{path.stem}.txt"
            label = np.loadtxt(label_path).reshape(-1)
            target = order_points(label[5:].reshape(4, 3)[:, :2] * [w, h])
            result = model.predict(image, conf=0.25, imgsz=640, device="cpu", verbose=False)[0]
            if (
                len(result.boxes) != 1
                or result.keypoints is None
                or result.keypoints.conf.min() < 0.4
            ):
                errors.append(1.0)
                missing += 1
                continue
            points = order_points(result.keypoints.xy[0].cpu().numpy())
            confidence = result.keypoints.conf[0].cpu().numpy()
            bounds = result.boxes.xyxy[0].cpu().numpy().reshape(2, 2)
            try:
                if touches_border(bounds, (h, w), config.card_yolo_border_ratio):
                    raise ValueError("bbox touches border")
                rectify_corners(image, points, confidence, config)
            except (EkycError, ValueError):
                errors.append(1.0)
                invalid += 1
                continue
            distance = min(
                np.linalg.norm(np.roll(target, shift, axis=0) - points, axis=1).mean()
                for shift in range(4)
            ) / np.hypot(h, w)
            errors.append(float(distance))
            valid_errors.append(float(distance))
        scores.append(
            {
                "path": str(candidate),
                "mean_error": float(np.mean(errors)),
                "median_error": float(np.median(errors)),
                "p90_error": float(np.percentile(errors, 90)),
                "missing": missing,
                "invalid_geometry": invalid,
                "valid_mean_corner_error": float(np.mean(valid_errors)) if valid_errors else None,
                "images": len(images),
            }
        )
    selected = min(scores, key=lambda score: score["mean_error"])
    output.parent.mkdir(parents=True, exist_ok=True)
    if Path(selected["path"]).resolve() != output.resolve():
        shutil.copy2(selected["path"], output)
    report = {
        "selected": selected,
        "candidates": scores,
        "checkpoint_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "note": (
            "Mean error includes penalty 1.0 for missing or invalid geometry; "
            "approximate validation labels; no independent test set."
        ),
    }
    output.with_suffix(".selection.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("candidates", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(select_checkpoint(args.data, args.candidates, args.output), indent=2))


if __name__ == "__main__":
    main()
