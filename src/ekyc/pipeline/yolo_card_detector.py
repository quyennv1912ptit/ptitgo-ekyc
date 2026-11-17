import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Lock

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.card_detector import CardDetectionResult, order_points, warp_card
from ekyc.pipeline.geometry import any_corner_outside, card_native_geometry


class DetectionStatus(str, Enum):  # noqa: UP042 - Giữ kiểu enum theo đặc tả.
    DETECTED = "DETECTED"
    NOT_FOUND = "NOT_FOUND"
    MULTIPLE_CARDS = "MULTIPLE_CARDS"


@dataclass(frozen=True)
class CardDetection:
    """Một thẻ dự đoán, tọa độ đã map về ảnh gốc và keypoint đã chuẩn hóa thứ tự."""

    quad_native: np.ndarray
    box_native: np.ndarray
    confidence: float
    rotation: int
    keypoint_confidence: np.ndarray | None = None


@dataclass(frozen=True)
class DetectionOutcome:
    status: DetectionStatus
    detection: CardDetection | None
    rotation: int
    passes_run: int
    ignored_detections: int
    error_code: ErrorCode | None
    failure_reason: str | None = None


# Thứ tự pass: 0 độ, 90 độ CW, 90 độ CCW. Mỗi cặp là (góc xoay, field ngưỡng conf).
ROTATION_PASSES: tuple[tuple[int, str], ...] = (
    (0, "card_conf_primary"),
    (90, "card_conf_rotated"),
    (-90, "card_conf_rotated"),
)


def rotate_frame(image: np.ndarray, rotation: int) -> np.ndarray:
    if rotation == 90:
        return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
    if rotation == -90:
        return cv2.rotate(image, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return image


def map_point(
    x_rotated: float, y_rotated: float, rotation: int, width: int, height: int
) -> tuple[float, float]:
    """Map điểm từ ảnh đã xoay về ảnh gốc theo quy ước pixel index."""
    if rotation == 90:
        return y_rotated, height - 1 - x_rotated
    if rotation == -90:
        return width - 1 - y_rotated, x_rotated
    return x_rotated, y_rotated


def map_box(box: np.ndarray, rotation: int, width: int, height: int) -> np.ndarray:
    """Map 4 góc của bbox rồi lấy min/max, không map trực tiếp x1/y1/x2/y2."""
    x1, y1, x2, y2 = (float(value) for value in box[:4])
    corners = ((x1, y1), (x2, y1), (x2, y2), (x1, y2))
    mapped = [map_point(x, y, rotation, width, height) for x, y in corners]
    xs = [point[0] for point in mapped]
    ys = [point[1] for point in mapped]
    return np.array([min(xs), min(ys), max(xs), max(ys)], dtype=np.float32)


def canonical_indices(points: np.ndarray) -> np.ndarray:
    """Thứ tự keypoint của nhãn train: bắt đầu gần góc trên-trái, đi theo chiều kim đồng hồ."""
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    order = np.argsort(angles)
    start = int(np.argmin(points[order].sum(axis=1)))
    return np.roll(order, -start)


def canonicalize(
    points: np.ndarray,
    keypoint_confidence: np.ndarray,
    box: np.ndarray,
    confidence: float,
    rotation: int,
    width: int,
    height: int,
) -> CardDetection:
    mapped = np.array(
        [
            map_point(x, y, rotation, width, height)
            for x, y in zip(points[:, 0], points[:, 1], strict=True)
        ],
        dtype=np.float32,
    )
    indices = canonical_indices(mapped)
    return CardDetection(
        quad_native=mapped[indices],
        box_native=map_box(box, rotation, width, height),
        confidence=float(confidence),
        rotation=rotation,
        keypoint_confidence=np.asarray(keypoint_confidence, np.float32)[indices],
    )


def is_valid_detection(det: CardDetection, threshold: float, settings: Settings) -> bool:
    """Hình học hợp lệ: đủ 4 đỉnh, lồi, aspect trong khoảng cho phép."""
    if det.confidence < threshold or not np.isfinite(det.quad_native).all():
        return False
    quad = det.quad_native.astype(np.float32)
    if len(np.unique(quad, axis=0)) != 4:
        return False
    if not cv2.isContourConvex(quad.reshape(-1, 1, 2)):
        return False
    area = float(cv2.contourArea(quad))
    if area <= 0:
        return False
    aspect = card_native_geometry(quad, 10**9, 10**9).aspect
    return bool(settings.card_aspect_min <= aspect <= settings.card_aspect_max)


def _hull(quad: np.ndarray) -> np.ndarray:
    return cv2.convexHull(np.asarray(quad, np.float32))


def quad_area(quad: np.ndarray) -> float:
    return float(cv2.contourArea(_hull(quad)))


def quad_intersection(first: np.ndarray, second: np.ndarray) -> float:
    if quad_area(first) <= 0 or quad_area(second) <= 0:
        return 0.0
    return float(cv2.intersectConvexConvex(_hull(first), _hull(second))[0])


def quad_overlap(first: np.ndarray, second: np.ndarray) -> float:
    """intersection / min(area), dùng để phát hiện lồng nhau."""
    denominator = min(quad_area(first), quad_area(second))
    return quad_intersection(first, second) / denominator if denominator > 0 else 0.0


def quad_iou(first: np.ndarray, second: np.ndarray) -> float:
    inter = quad_intersection(first, second)
    union = quad_area(first) + quad_area(second) - inter
    return inter / union if union > 0 else 0.0


def same_card(first: CardDetection, second: CardDetection, settings: Settings) -> bool:
    return bool(
        quad_iou(first.quad_native, second.quad_native) >= settings.card_dup_iou
        or quad_overlap(first.quad_native, second.quad_native) >= settings.card_contain_ratio
    )


def select_best_detection(
    detections: list[CardDetection], settings: Settings
) -> tuple[CardDetection | None, bool, int]:
    """Gom cụm cùng thẻ, trả (thẻ tốt nhất, có thẻ thứ hai đáng kể, số detection bỏ qua)."""
    clusters: list[list[CardDetection]] = []
    for detection in sorted(
        detections, key=lambda item: item.confidence * quad_area(item.quad_native), reverse=True
    ):
        for cluster in clusters:
            if same_card(detection, cluster[0], settings):
                cluster.append(detection)
                break
        else:
            clusters.append([detection])
    if not clusters:
        return None, False, 0

    best = clusters[0][0]
    multiple = False
    ignored = 0
    for cluster in clusters[1:]:
        candidate = cluster[0]
        if (
            quad_overlap(best.quad_native, candidate.quad_native)
            >= settings.card_secondary_max_overlap
        ):
            # Chồng lấn nhưng không đủ để gộp: mơ hồ, giữ thẻ tốt nhất.
            ignored += 1
            continue
        if candidate.confidence >= settings.card_secondary_min_conf and quad_area(
            candidate.quad_native
        ) >= settings.card_secondary_min_area_ratio * quad_area(best.quad_native):
            multiple = True
        else:
            ignored += 1
    return best, multiple, ignored


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

    def predict_pass(
        self, image: np.ndarray, rotation: int, threshold: float
    ) -> list[CardDetection]:
        frame = rotate_frame(image, rotation)
        height, width = image.shape[:2]
        with self._lock:
            results = self.model.predict(
                frame,
                imgsz=self.settings.card_yolo_imgsz,
                conf=threshold,
                iou=0.7,
                device="cpu",
                verbose=False,
                save=False,
            )
        if not results:
            return []
        result = results[0]
        if result.boxes is None:
            return []
        boxes = result.boxes.xyxy.cpu().numpy()
        boxes_conf = result.boxes.conf.cpu().numpy()
        if result.keypoints is None or result.keypoints.conf is None:
            return [
                CardDetection(
                    np.empty((0, 2)), map_box(box, rotation, width, height), float(score), rotation
                )
                for box, score in zip(boxes, boxes_conf, strict=True)
            ]
        points = result.keypoints.xy.cpu().numpy()
        keypoint_conf = result.keypoints.conf.cpu().numpy()
        detections = []
        for index in range(min(len(boxes), len(points), len(keypoint_conf))):
            detections.append(
                canonicalize(
                    np.asarray(points[index], np.float32),
                    np.asarray(keypoint_conf[index], np.float32),
                    np.asarray(boxes[index], np.float32),
                    float(boxes_conf[index]),
                    rotation,
                    width,
                    height,
                )
            )
        return detections

    def detect_cards(self, image: np.ndarray) -> DetectionOutcome:
        passes_run = 0
        saw_box = False
        for rotation, threshold_field in ROTATION_PASSES:
            passes_run += 1
            threshold = getattr(self.settings, threshold_field)
            raw = self.predict_pass(image, rotation, threshold)
            saw_box |= bool(raw)
            valid = [
                detection
                for detection in raw
                if is_valid_detection(detection, threshold, self.settings)
            ]
            if not valid:
                continue
            best, multiple, ignored = select_best_detection(valid, self.settings)
            if multiple:
                return DetectionOutcome(
                    DetectionStatus.MULTIPLE_CARDS,
                    None,
                    rotation,
                    passes_run,
                    ignored,
                    ErrorCode.MULTIPLE_CARDS,
                )
            return DetectionOutcome(
                DetectionStatus.DETECTED, best, rotation, passes_run, ignored, None
            )
        return DetectionOutcome(
            DetectionStatus.NOT_FOUND,
            None,
            0,
            passes_run,
            0,
            ErrorCode.CARD_NOT_FOUND,
            "KEYPOINT_INVALID_GEOMETRY" if saw_box else "DETECTOR_NO_BOX",
        )

    def detect(self, image: np.ndarray) -> CardDetectionResult:
        outcome = self.detect_cards(image)
        if outcome.status is not DetectionStatus.DETECTED or outcome.detection is None:
            raise EkycError(outcome.error_code or ErrorCode.CARD_NOT_FOUND)
        detection = outcome.detection
        confidence = (
            detection.keypoint_confidence
            if detection.keypoint_confidence is not None
            else np.ones(4, np.float32)
        )
        return rectify_corners(image, detection.quad_native, confidence, self.settings)


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
    # Góc nằm ngoài ảnh thì không thể nắn; ngưỡng 5 px do gate hình học quyết định.
    if any_corner_outside(corners, width, height):
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
