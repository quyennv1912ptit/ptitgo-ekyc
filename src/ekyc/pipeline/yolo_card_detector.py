import os
from pathlib import Path
from threading import Lock

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.card_detector import CardDetectionResult, order_points, warp_card


class YoloCardDetector:
    """Một model pose 4 góc dùng lại giữa các request, không dùng model COCO mặc định."""

    def __init__(self, settings: Settings) -> None:
        path = Path(settings.card_yolo_model_path)
        if not path.is_file():
            raise FileNotFoundError(f"Card checkpoint missing: {path}")
        os.environ.setdefault("YOLO_CONFIG_DIR", str(Path("models/ultralytics_settings").resolve()))
        os.environ.setdefault("YOLO_OFFLINE", "true")
        from ultralytics import YOLO
        from ultralytics import settings as yolo_settings

        yolo_settings.update({"sync": False})
        self.model = YOLO(str(path), task="pose")
        if self.model.task != "pose" or self.model.model.yaml.get("kpt_shape") != [4, 3]:
            raise ValueError("Card model must have four corner keypoints with confidence")
        if self.model.names != {0: "card"}:
            raise ValueError("Card model must have exactly one class named card")
        self.settings = settings
        self._lock = Lock()

    def detect(self, image: np.ndarray) -> CardDetectionResult:
        with self._lock:
            results = self.model.predict(
                image,
                imgsz=self.settings.card_yolo_imgsz,
                conf=self.settings.card_yolo_conf,
                device="cpu",
                verbose=False,
                save=False,
            )
        if not results or results[0].boxes is None or len(results[0].boxes) == 0:
            raise EkycError(ErrorCode.CARD_NOT_FOUND)
        result = results[0]
        if len(result.boxes) != 1:
            raise EkycError(ErrorCode.CARD_NOT_FOUND, "Ảnh có nhiều thẻ, hãy chụp riêng một thẻ.")
        # Góc pose có thể được model suy đoán vào trong ảnh dù thẻ đã mất góc.
        # Chặn thêm khi bbox chạm mép, tránh nắn từ những góc được suy đoán này.
        bounds = result.boxes.xyxy[0].cpu().numpy().reshape(2, 2)
        if self.settings.quality_profile != "calibration" and touches_border(
            bounds, image.shape[:2], self.settings.card_yolo_border_ratio
        ):
            raise EkycError(ErrorCode.CARD_CROPPED)
        if result.keypoints is None or result.keypoints.conf is None:
            raise EkycError(ErrorCode.CARD_NOT_FOUND)
        points = result.keypoints.xy[0].cpu().numpy().astype(np.float32)
        confidence = result.keypoints.conf[0].cpu().numpy()
        return rectify_corners(image, points, confidence, self.settings)


def rectify_corners(
    image: np.ndarray, points: np.ndarray, confidence: np.ndarray, settings: Settings
) -> CardDetectionResult:
    height, width = image.shape[:2]
    if (
        points.shape != (4, 2)
        or confidence.shape != (4,)
        or not np.isfinite(points).all()
        or not np.isfinite(confidence).all()
        or np.any(confidence < settings.card_yolo_keypoint_conf)
    ):
        raise EkycError(ErrorCode.CARD_NOT_FOUND)
    if len(np.unique(points, axis=0)) != 4:
        raise EkycError(ErrorCode.CARD_NOT_FOUND)
    corners = order_points(points)
    if not cv2.isContourConvex(corners):
        raise EkycError(ErrorCode.CARD_NOT_FOUND)
    area = cv2.contourArea(corners)
    if area < settings.card_yolo_min_area_ratio * width * height:
        raise EkycError(ErrorCode.CARD_NOT_FOUND)
    if settings.quality_profile != "calibration" and touches_border(
        corners, (height, width), settings.card_yolo_border_ratio
    ):
        raise EkycError(ErrorCode.CARD_CROPPED)
    warped = warp_card(image, corners)
    h, w = warped.shape[:2]
    ratio = w / h
    if not settings.card_aspect_ratio_min <= ratio <= settings.card_aspect_ratio_max:
        raise EkycError(ErrorCode.CARD_NOT_FOUND)
    return CardDetectionResult(warped, corners, w, h, ratio)


def touches_border(points: np.ndarray, shape: tuple[int, int], ratio: float) -> bool:
    height, width = shape
    margin = ratio * min(height, width)
    return bool(
        points[:, 0].min() <= margin
        or points[:, 1].min() <= margin
        or points[:, 0].max() >= width - 1 - margin
        or points[:, 1].max() >= height - 1 - margin
    )
