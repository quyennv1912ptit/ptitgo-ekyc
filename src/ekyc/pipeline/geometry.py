import math
from dataclasses import dataclass
from enum import Enum

import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import ErrorCode, WarningCode
from ekyc.schemas.common import Warning

LOW_RESOLUTION_MESSAGE = "Độ phân giải ảnh thấp, hãy kiểm tra lại các trường được trích xuất."
IMAGE_TOO_SMALL_MESSAGE = (
    "Ảnh quá nhỏ hoặc giấy tờ ở quá xa, hãy đưa giấy tờ lại gần máy ảnh "
    "và chụp ở độ phân giải đầy đủ."
)


class GateStatus(str, Enum):  # noqa: UP042 - Giữ kiểu enum theo đặc tả.
    PASS = "PASS"
    PASS_REVIEW = "PASS_REVIEW"
    BLOCK = "BLOCK"


@dataclass(frozen=True)
class CardGeometry:
    """Hình học thẻ đo trên ảnh gốc (đã xoay EXIF), trước khi nắn."""

    long_side_px: float
    short_side_px: float
    aspect: float
    min_border_distance_px: float
    any_corner_outside: bool


@dataclass(frozen=True)
class GateResult:
    status: GateStatus
    error_code: ErrorCode | None
    warnings: tuple[Warning, ...]
    geometry: CardGeometry


def _cyclic_order(points: np.ndarray) -> np.ndarray:
    """Xếp 4 điểm vòng quanh tâm để cạnh nối đúng cặp kề nhau, không phụ thuộc thứ tự keypoint."""
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    return points[np.argsort(angles)]


def any_corner_outside(quad: np.ndarray, image_w: int, image_h: int) -> bool:
    """Điểm góc nằm ngoài vùng pixel hợp lệ [0, W-1] x [0, H-1]."""
    points = np.asarray(quad, dtype=np.float64).reshape(4, 2)
    return bool(
        np.any(points[:, 0] < 0)
        or np.any(points[:, 1] < 0)
        or np.any(points[:, 0] > image_w - 1)
        or np.any(points[:, 1] > image_h - 1)
    )


def experimental_geometry_warnings(quad: np.ndarray, settings: Settings) -> tuple[Warning, ...]:
    """Flag unusual pose geometry for review using the prior offline candidate limits."""
    if not settings.card_experimental_geometry_review:
        return ()
    ordered = _cyclic_order(np.asarray(quad, dtype=np.float64))
    edges = np.roll(ordered, -1, axis=0) - ordered
    lengths = np.linalg.norm(edges, axis=1)
    if np.isfinite(lengths).all() and np.all(lengths > 0):
        units = edges / lengths[:, None]
        cosine = -np.sum(units * np.roll(units, 1, axis=0), axis=1)
        ratios = lengths[:2] / lengths[2:]
        if np.all(np.abs(cosine) <= 0.5 + 1e-12) and np.all(np.maximum(ratios, 1 / ratios) <= 1.25):
            return ()
    return (
        Warning(
            code=WarningCode.CARD_GEOMETRY_UNCERTAIN,
            message="Hình dạng vùng thẻ bất thường. Cần kiểm tra thủ công trước khi xác nhận.",
        ),
    )


def card_native_geometry(quad: np.ndarray, image_w: int, image_h: int) -> CardGeometry:
    """Đo cạnh dài/ngắn bằng trung bình hai cạnh đối diện, đúng cho thẻ dọc và thẻ xoay."""
    points = np.asarray(quad, dtype=np.float64).reshape(4, 2)
    ordered = _cyclic_order(points)
    edges = [float(np.linalg.norm(ordered[(index + 1) % 4] - ordered[index])) for index in range(4)]
    first_pair = (edges[0] + edges[2]) / 2
    second_pair = (edges[1] + edges[3]) / 2
    long_side, short_side = max(first_pair, second_pair), min(first_pair, second_pair)
    border = np.minimum.reduce(
        [points[:, 0], points[:, 1], (image_w - 1) - points[:, 0], (image_h - 1) - points[:, 1]]
    )
    return CardGeometry(
        long_side_px=long_side,
        short_side_px=short_side,
        aspect=long_side / short_side if short_side > 0 else math.inf,
        min_border_distance_px=float(border.min()),
        any_corner_outside=any_corner_outside(points, image_w, image_h),
    )


def evaluate_card_gate(
    quad: np.ndarray,
    image_w: int,
    image_h: int,
    settings: Settings,
) -> GateResult:
    """Gate hình học trước warp: chặn thẻ bị xén, cảnh báo độ phân giải thấp."""
    geometry = card_native_geometry(quad, image_w, image_h)
    # Chỉ 4 điểm góc quyết định "bị xén"; bbox chạm biên không tính.
    if (
        geometry.any_corner_outside
        or geometry.min_border_distance_px < settings.card_edge_margin_px
    ):
        return GateResult(GateStatus.BLOCK, ErrorCode.CARD_CROPPED, (), geometry)
    if geometry.long_side_px < settings.card_min_long_side_px:
        return GateResult(GateStatus.BLOCK, ErrorCode.IMAGE_TOO_SMALL, (), geometry)
    if geometry.long_side_px < settings.card_ok_long_side_px:
        warning = Warning(code=WarningCode.LOW_RESOLUTION, message=LOW_RESOLUTION_MESSAGE)
        return GateResult(GateStatus.PASS_REVIEW, None, (warning,), geometry)
    return GateResult(GateStatus.PASS, None, (), geometry)
