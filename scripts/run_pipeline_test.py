"""Run Quality and Card Detector modules on tests/data images and save outputs to tests/output."""

import json
import logging
from pathlib import Path

import cv2
import numpy as np

from ekyc.config import get_settings
from ekyc.core.errors import EkycError
from ekyc.pipeline.card_detector import (
    find_card_corners,
    order_points,
    warp_card,
)
from ekyc.pipeline.quality import inspect_image_quality

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("pipeline_runner")

DATA_DIR = Path("tests/data")
OUTPUT_DIR = Path("tests/output")


def annotate_corners(image: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Draw polygon and numbered corner points on image for visualization."""
    annotated = image.copy()
    pts = np.int32(order_points(corners))

    # Vẽ khung đa giác nối 4 góc
    cv2.polylines(annotated, [pts], isClosed=True, color=(0, 255, 0), thickness=3)

    corner_names = ["1:TL", "2:TR", "3:BR", "4:BL"]
    colors = [(0, 0, 255), (0, 255, 255), (255, 0, 0), (255, 0, 255)]

    for _idx, (pt, name, col) in enumerate(zip(pts, corner_names, colors, strict=False)):
        cv2.circle(annotated, tuple(pt), radius=8, color=col, thickness=-1)
        cv2.putText(
            annotated,
            name,
            (pt[0] + 10, pt[1] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            3,
            cv2.LINE_AA,
        )
        cv2.putText(
            annotated,
            name,
            (pt[0] + 10, pt[1] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            col,
            2,
            cv2.LINE_AA,
        )

    return annotated


def process_image(img_path: Path, settings) -> dict:
    """Run Quality and Card Detector on a single image and save visual artifacts."""
    logger.info("Đang xử lý ảnh: %s", img_path.name)
    raw_bytes = img_path.read_bytes()
    nparr = np.frombuffer(raw_bytes, np.uint8)
    image = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

    if image is None:
        return {
            "file": img_path.name,
            "status": "ERROR",
            "error": "INVALID_IMAGE",
            "message": "Không thể giải mã file ảnh",
        }

    h, w = image.shape[:2]

    # 1. Chạy module Quality
    quality_metrics, quality_errors = inspect_image_quality(image, settings)
    quality_report = {
        "width": w,
        "height": h,
        "min_side_px": quality_metrics.min_side_px,
        "brightness": round(quality_metrics.brightness, 2),
        "glare_ratio": round(quality_metrics.glare_ratio, 4),
        "blur_score": round(quality_metrics.blur_score, 2),
        "errors": [err.value for err in quality_errors],
        "is_passed": len(quality_errors) == 0,
    }

    # 2. Chạy module Card Detector
    card_report = {
        "detected": False,
        "is_cropped": False,
        "corners": None,
        "warped_size": None,
        "aspect_ratio": None,
        "error": None,
    }

    base_name = img_path.stem
    try:
        corners = find_card_corners(image, min_card_area_ratio=0.10)
        card_report["detected"] = True
        card_report["corners"] = corners.tolist()

        # Lưu ảnh vẽ 4 góc thẻ
        annotated = annotate_corners(image, corners)
        annotated_path = OUTPUT_DIR / f"{base_name}_annotated.jpg"
        cv2.imwrite(str(annotated_path), annotated)
        card_report["annotated_file"] = annotated_path.name

        # Warp thẻ
        warped = warp_card(image, corners)
        warped_h, warped_w = warped.shape[:2]
        card_report["warped_size"] = f"{warped_w}x{warped_h}"
        card_report["aspect_ratio"] = round(warped_w / max(warped_h, 1), 3)

        warped_path = OUTPUT_DIR / f"{base_name}_warped.jpg"
        cv2.imwrite(str(warped_path), warped)
        card_report["warped_file"] = warped_path.name

    except EkycError as exc:
        card_report["error"] = exc.code.value
        card_report["message"] = exc.message
        if exc.code.value == "CARD_CROPPED":
            card_report["is_cropped"] = True

    return {
        "file": img_path.name,
        "quality": quality_report,
        "card_detector": card_report,
    }


def main():
    settings = get_settings()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    image_files = sorted(
        [p for p in DATA_DIR.iterdir() if p.is_file() and p.suffix.lower() in image_extensions]
    )

    if not image_files:
        logger.warning(
            "Thư mục '%s' hiện chưa có file ảnh nào (hỗ trợ %s)!",
            DATA_DIR.resolve(),
            ", ".join(image_extensions),
        )
        return

    logger.info("Tìm thấy %d ảnh trong %s. Bắt đầu xử lý...", len(image_files), DATA_DIR)
    results = []
    for img_path in image_files:
        res = process_image(img_path, settings)
        results.append(res)

    report_path = OUTPUT_DIR / "summary_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    logger.info("Đã xử lý xong! Báo cáo tóm tắt lưu tại: %s", report_path)


if __name__ == "__main__":
    main()
