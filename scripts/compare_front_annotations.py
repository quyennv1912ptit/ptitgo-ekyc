"""Compare manual four-corner rectification with local YOLO; private local OCR diagnostics."""

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import EkycError
from ekyc.pipeline.card_detector import order_points, warp_card
from ekyc.pipeline.card_image import quality_settings
from ekyc.pipeline.doc_classifier import DocClassifier
from ekyc.pipeline.image_loader import load_image_from_bytes
from ekyc.pipeline.ocr.easyocr_engine import EasyOcrEngine
from ekyc.pipeline.ocr.line_grouping import group_into_lines
from ekyc.pipeline.parsers.cccd_parser import ParseDebug, parse_cccd_front
from ekyc.pipeline.quality import inspect_image_quality
from ekyc.pipeline.yolo_card_detector import YoloCardDetector
from ekyc.services.cccd_service import resize_for_ocr


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def manual_corners(label: dict, shape: tuple[int, ...]) -> np.ndarray:
    height, width = shape[:2]
    if [label.get("imageWidth"), label.get("imageHeight")] != [width, height]:
        raise ValueError("annotation_dimensions_differ_from_decoded_image")
    shapes = label.get("shapes", [])
    if len(shapes) != 1 or shapes[0].get("label") != "cccd_front":
        raise ValueError("expected_one_cccd_front_polygon")
    points = np.asarray(shapes[0].get("points", []), dtype=np.float32)
    if points.shape != (4, 2):
        raise ValueError(f"expected_four_points_got_{len(points)}")
    if (
        not np.isfinite(points).all()
        or not cv2.isContourConvex(points)
        or cv2.contourArea(points) < 1
        or np.any(points < 0)
        or np.any(points[:, 0] >= width)
        or np.any(points[:, 1] >= height)
    ):
        raise ValueError("invalid_or_outside_quadrilateral")
    return order_points(points)


def quality(image: np.ndarray, settings: Settings) -> dict:
    result = {}
    for name in ("strict", "evaluation"):
        config = quality_settings(settings.model_copy(update={"quality_profile": name}))
        metrics, errors = inspect_image_quality(image, config)
        result[name] = {"metrics": asdict(metrics), "errors": [error.value for error in errors]}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--model", type=Path, default=Path("models/card_yolov8n_pose.selected.pt"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    settings = Settings(ekyc_api_key="local-comparison", card_yolo_model_path=str(args.model))
    images = sorted(
        p
        for p in args.folder.iterdir()
        if p.suffix.lower()
        in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
        }
    )
    if not images:
        raise ValueError("No input images")
    args.output.mkdir(parents=True, exist_ok=True)
    metadata = {
        "model": str(args.model.resolve()),
        "model_sha256": digest(args.model),
        "settings": settings.model_dump(mode="json", exclude={"ekyc_api_key"}),
        "source_sha256": {str(p): digest(p) for p in sorted(Path("src/ekyc").rglob("*.py"))},
        "script_sha256": digest(Path(__file__)),
        "inputs": [
            {
                "id": f"F{i:02}",
                "path": str(p),
                "sha256": digest(p),
                "label_sha256": digest(p.with_suffix(".json"))
                if p.with_suffix(".json").is_file()
                else None,
            }
            for i, p in enumerate(images, 1)
        ],
        "note": (
            "Diagnostic OCR/parser bypass quality/classification gates; "
            "YOLO geometry remains enforced."
        ),
    }
    meta_path = args.output / "run_config.json"
    if meta_path.exists() and json.loads(meta_path.read_text(encoding="utf-8")) != metadata:
        raise ValueError("Inputs/code/settings changed; use a new output directory")
    save_json(meta_path, metadata)
    detector = YoloCardDetector(settings)
    standard_size = (settings.card_output_width, settings.card_output_height)
    rows = []
    for entry in metadata["inputs"]:
        directory = args.output / entry["id"]
        directory.mkdir(exist_ok=True)
        result_path = directory / "result.json"
        if result_path.exists():
            cached = json.loads(result_path.read_text(encoding="utf-8"))
            if cached.get("complete"):
                rows.append(cached)
                continue
        image = load_image_from_bytes(Path(entry["path"]).read_bytes(), settings).bgr
        row = {**entry, "variants": {}, "image_shape": list(image.shape), "ocr_complete": False}
        overlay = image.copy()
        manual = None
        try:
            annotation = json.loads(
                Path(entry["path"]).with_suffix(".json").read_text(encoding="utf-8-sig")
            )
            manual = manual_corners(annotation, image.shape)
            native = warp_card(image, manual)
            row["manual"] = {
                "status": "ok",
                "corners": manual.tolist(),
                "quality": quality(native, settings),
            }
            cv2.imwrite(
                str(directory / "manual.png"),
                cv2.resize(native, standard_size, interpolation=cv2.INTER_AREA),
            )
            cv2.polylines(overlay, [manual.astype(np.int32)], True, (0, 255, 0), 8)
        except (ValueError, OSError) as error:
            row["manual"] = {"status": "invalid_annotation", "reason": str(error)}
            cv2.imwrite(str(directory / "raw.png"), resize_for_ocr(image, settings.ocr_max_side_px))
        try:
            detected = detector.detect(image)
            row["yolo"] = {
                "status": "ok",
                "corners": detected.corners.tolist(),
                "quality": quality(detected.warped_image, settings),
            }
            cv2.imwrite(
                str(directory / "yolo.png"),
                cv2.resize(detected.warped_image, standard_size, interpolation=cv2.INTER_AREA),
            )
            cv2.polylines(overlay, [detected.corners.astype(np.int32)], True, (0, 0, 255), 8)
            if manual is not None:
                intersections, _ = cv2.intersectConvexConvex(manual, detected.corners)
                manual_area, yolo_area = cv2.contourArea(manual), cv2.contourArea(detected.corners)
                row["geometry"] = {
                    "corner_error_diagonal": float(
                        np.linalg.norm(manual - detected.corners, axis=1).mean()
                        / np.hypot(*image.shape[:2])
                    ),
                    "manual_area_coverage": intersections / manual_area,
                    "yolo_area_on_card": intersections / yolo_area,
                    "polygon_iou": intersections / (manual_area + yolo_area - intersections),
                }
        except EkycError as error:
            row["yolo"] = {"status": error.code.value}
        cv2.imwrite(str(directory / "overlay.jpg"), resize_for_ocr(overlay, 1600))
        save_json(result_path, row)
        rows.append(row)
        print(entry["id"], "prepared", row["manual"]["status"], row["yolo"]["status"], flush=True)
    import torch

    torch.set_num_threads(args.threads)
    engine = EasyOcrEngine(settings.ocr_languages, width_ths=settings.ocr_width_ths)
    torch.set_num_threads(args.threads)
    for row in rows:
        if row.get("complete"):
            continue
        directory = args.output / row["id"]
        for name in ("manual", "yolo", "raw"):
            path = directory / f"{name}.png"
            if not path.exists():
                continue
            started = perf_counter()
            image = cv2.imread(str(path))
            boxes = engine.read_lines(image)
            lines = group_into_lines(
                boxes, row_tolerance=settings.line_row_tolerance, gap_ratio=settings.line_gap_ratio
            )
            debug = ParseDebug()
            parsed = parse_cccd_front(lines, settings, date(2026, 11, 10), debug)
            row["variants"][name] = {
                "shape": list(image.shape),
                "seconds": perf_counter() - started,
                "boxes": [asdict(box) for box in boxes],
                "lines": [asdict(line) for line in lines],
                "classification": asdict(DocClassifier(settings).classify(lines, image.shape[0])),
                "parsed": asdict(parsed),
                "parse_debug": asdict(debug),
            }
            (directory / f"{name}_ocr.txt").write_text(
                "\n".join(box.text for box in boxes), encoding="utf-8"
            )
            save_json(directory / "result.json", row)
            print(row["id"], name, "ocr_done", round(perf_counter() - started, 1), flush=True)
        row["ocr_complete"] = True
        row["complete"] = True
        save_json(directory / "result.json", row)
    save_json(args.output / "results.json", rows)
    print("Completed", len(rows), "images", flush=True)


if __name__ == "__main__":
    main()
