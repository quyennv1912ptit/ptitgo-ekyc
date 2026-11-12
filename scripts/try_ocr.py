import argparse
from dataclasses import fields
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.logging import setup_logging
from ekyc.pipeline.card_detector import detect_and_warp_card
from ekyc.pipeline.card_image import DefaultCardPreparer
from ekyc.pipeline.doc_classifier import DocClassifier
from ekyc.pipeline.ocr.easyocr_engine import EasyOcrEngine
from ekyc.pipeline.ocr.line_grouping import group_into_lines
from ekyc.pipeline.parsers.cccd_checks import (
    REVIEW_ORDER,
    check_id_structure,
    collect_review_fields,
)
from ekyc.pipeline.parsers.cccd_parser import ParseDebug, parse_cccd_back, parse_cccd_front
from ekyc.services.cccd_service import resize_for_ocr


def prepare_local_image(
    image_path: Path, *, rotate: int, warp: bool, max_side: int, settings: Settings | None = None
) -> np.ndarray:
    image = cv2.imread(str(image_path))
    if image is None:
        raise ValueError("Không đọc được ảnh")
    rotations = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }
    if rotate:
        image = cv2.rotate(image, rotations[rotate])
    if warp:
        if settings is None:
            settings = Settings(ekyc_api_key="local-cli")
        preparer = DefaultCardPreparer(settings)
        detected = (
            preparer.detector.detect(image)
            if preparer.detector is not None
            else detect_and_warp_card(image)
        )
        image = preparer.finish(detected.warped_image)
    return resize_for_ocr(image, max_side)


def main() -> None:
    settings = Settings(ekyc_api_key="x")
    setup_logging(settings.log_level)
    print(
        "Công cụ chạy cục bộ, output chứa dữ liệu cá nhân, đừng dán lên nơi công khai", flush=True
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--side", choices=["front", "back"], default="front")
    parser.add_argument(
        "--rotate",
        type=int,
        choices=[0, 90, 180, 270],
        default=0,
        help="Góc xoay theo chiều kim đồng hồ trước OCR",
    )
    parser.add_argument(
        "--warp",
        action="store_true",
        help="Chạy detector đã cấu hình, nắn thẻ và quality gate trước OCR",
    )
    parser.add_argument(
        "--width-ths",
        type=float,
        default=None,
        help="Ghi đè OCR_WIDTH_THS, ví dụ 0.2 nếu box thô gộp cột",
    )
    args = parser.parse_args()
    image = prepare_local_image(
        args.image,
        rotate=args.rotate,
        warp=args.warp,
        max_side=settings.ocr_max_side_px,
        settings=settings,
    )
    width_ths = args.width_ths if args.width_ths is not None else settings.ocr_width_ths
    engine = EasyOcrEngine(settings.ocr_languages, width_ths=width_ths)
    boxes = engine.read_lines(image)
    print("Box thô:")
    for index, box in enumerate(boxes):
        print(
            f"[{index}] x1={box.x1 / image.shape[1]:.3f} x2={box.x2 / image.shape[1]:.3f} "
            f"y={box.center_y / image.shape[0]:.3f} conf={box.conf:.3f} {box.text}"
        )
    lines = group_into_lines(
        boxes,
        row_tolerance=settings.line_row_tolerance,
        gap_ratio=settings.line_gap_ratio,
    )
    print("Dòng đã gộp:")
    for index, line in enumerate(lines):
        print(
            f"[{index}] parts={len(line.parts)} y={line.center_y / image.shape[0]:.3f} "
            f"x1={line.x1 / image.shape[1]:.3f} "
            f"x2={line.x2 / image.shape[1]:.3f} conf={line.conf:.3f} {line.text}"
        )
    classification = DocClassifier(settings).classify(lines, image.shape[0])
    print(f"doc_type={classification.doc_type.value} side={classification.side.value}")
    print(f"classify_score (độ khớp từ khóa, KHÔNG phải độ chính xác OCR): {classification.score}")
    debug = ParseDebug()
    result = (
        parse_cccd_front(lines, settings, datetime.now(ZoneInfo(settings.timezone)).date(), debug)
        if args.side == "front"
        else parse_cccd_back(lines, settings)
    )
    aliases = {
        "id_number": "idNumber",
        "full_name": "fullName",
        "dob": "dob",
        "gender": "gender",
        "origin": "placeOfOrigin",
        "address": "address",
        "expiry": "expiryDate",
        "issue_date": "issueDate",
    }
    parsed_fields = {
        aliases[item.name]: getattr(result, item.name)
        for item in fields(result)
        if item.name in aliases
    }
    mismatches = []
    if args.side == "front" and result.id_number.value and result.dob.value:
        mismatches = check_id_structure(
            result.id_number.value, result.dob.value, result.gender.value
        )
    reviews = collect_review_fields(
        parsed_fields,
        settings,
        no_expiry=getattr(result, "no_expiry", False),
        mismatches=mismatches,
    )
    if args.side == "front" and not result.layout_reliable:
        review = set(reviews) | {"placeOfOrigin", "address"}
        reviews = [name for name in REVIEW_ORDER if name in review]
    right = debug.x_right / image.shape[1] if debug.x_right is not None else None
    split = debug.x_split / image.shape[1] if debug.x_split is not None else None
    print(
        f"x_right={right} x_split={split} "
        f"layout_reliable={getattr(result, 'layout_reliable', None)}"
    )
    print("Mảnh:")
    for index, item in enumerate(debug.fragments):
        print(
            f"[{index}] column={item.column} source={item.source} estimated={item.estimated} "
            f"x1={item.x1 / image.shape[1]:.3f} y={item.center_y / image.shape[0]:.3f} {item.text}"
        )
    reverse_aliases = {alias: key for key, alias in aliases.items()}
    for name, value in parsed_fields.items():
        print(
            f"{name}: status={value.status} conf={value.conf} review={name in reviews} "
            f"used_fragments={debug.used_fragments.get(reverse_aliases[name], ())} "
            f"raw={value.raw!r} value={value.value!r}"
        )
    print(f"reviewFields={reviews} reviewRequired={bool(reviews)}")
    print(f"scrubbed={debug.scrubbed}")
    print(f"label_splits={debug.label_splits}")


if __name__ == "__main__":
    main()
