"""Experimental review-only policy for uncertain pose predictions."""

from dataclasses import dataclass

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode, WarningCode
from ekyc.pipeline.card_detector import CardDetectionResult
from ekyc.pipeline.geometry import GateStatus, evaluate_card_gate
from ekyc.pipeline.yolo_card_detector import CardDetection, rectify_corners
from ekyc.schemas.common import Warning


@dataclass(frozen=True)
class RecoveryResult:
    crop: CardDetectionResult
    status: GateStatus
    warnings: tuple[Warning, ...]
    edge_support: float | None = None


def _failure(code: ErrorCode, reason: str) -> EkycError:
    return EkycError(code, details=[{"reason": reason}])


def _plausible(points: np.ndarray, box: np.ndarray, image_shape: tuple[int, int]) -> bool:
    height, width = image_shape
    if box.shape != (4,) or not np.isfinite(box).all():
        return False
    low, high = box[:2], box[2:]
    box_size = high - low
    if np.any(box_size <= 0) or np.any(points < low) or np.any(points > high):
        return False
    area = cv2.contourArea(points)
    if area < 0.05 * width * height:
        return False
    if np.any(np.ptp(points, axis=0) < 0.8 * box_size):
        return False
    edges = np.roll(points, -1, axis=0) - points
    lengths = np.linalg.norm(edges, axis=1)
    opposite = lengths[:2] / lengths[2:]
    if np.any(np.maximum(opposite, 1 / opposite) > 1.25):
        return False
    units = edges / lengths[:, None]
    cosine = -np.sum(units * np.roll(units, 1, axis=0), axis=1)
    return bool(np.all(np.abs(cosine) <= 0.5))


def measure_edge_support(image: np.ndarray, points: np.ndarray) -> float:
    """Fraction of sampled perimeter positions near a Canny edge. Diagnostic only."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.dilate(cv2.Canny(gray, 50, 150), np.ones((7, 7), np.uint8))
    samples = np.concatenate(
        [np.linspace(a, b, 48) for a, b in zip(points, np.roll(points, -1, axis=0), strict=True)]
    )
    x, y = np.rint(samples).astype(int).T
    return float(np.mean(edges[y, x] > 0))


def prepare_pose(image: np.ndarray, detection: CardDetection, settings: Settings) -> RecoveryResult:
    height, width = image.shape[:2]
    points = np.asarray(detection.quad_native, np.float32)
    confidence = np.asarray(detection.keypoint_confidence, np.float32)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise _failure(ErrorCode.CARD_NOT_FOUND, "KEYPOINT_INVALID_GEOMETRY")
    gate = evaluate_card_gate(points, width, height, settings)
    if gate.status is GateStatus.BLOCK:
        code = gate.error_code or ErrorCode.CARD_NOT_FOUND
        raise _failure(code, code.value)
    # Canonical detector output is cyclic. Reject crossings before warp can reorder them.
    if not cv2.isContourConvex(points) or np.any(np.all(points == 0, axis=1)):
        raise _failure(ErrorCode.CARD_NOT_FOUND, "KEYPOINT_INVALID_GEOMETRY")
    if confidence.shape != (4,) or not np.isfinite(confidence).all():
        raise _failure(ErrorCode.CARD_NOT_FOUND, "KEYPOINT_INVALID_GEOMETRY")
    uncertain = np.any(confidence < settings.card_yolo_keypoint_conf)
    if uncertain and (
        not settings.card_experimental_keypoint_recovery
        or np.count_nonzero(confidence < 0.1) >= 2
        or not _plausible(points, detection.box_native, (height, width))
        or not settings.card_aspect_ratio_min
        <= gate.geometry.aspect
        <= settings.card_aspect_ratio_max
    ):
        raise _failure(ErrorCode.CARD_NOT_FOUND, "KEYPOINT_LOW_CONFIDENCE")
    warp_confidence = np.ones_like(confidence) if uncertain else confidence
    try:
        crop = rectify_corners(image, points, warp_confidence, settings)
    except (EkycError, cv2.error) as error:
        raise _failure(ErrorCode.CARD_NOT_FOUND, "WARP_FAILED") from error
    if not uncertain:
        return RecoveryResult(crop, gate.status, gate.warnings)
    warning = Warning(
        code=WarningCode.LOW_KEYPOINT_CONFIDENCE,
        message="Không chắc chắn về vị trí góc thẻ. Cần kiểm tra thủ công hoặc chụp lại.",
    )
    return RecoveryResult(
        crop, GateStatus.PASS_REVIEW, (*gate.warnings, warning), measure_edge_support(image, points)
    )
