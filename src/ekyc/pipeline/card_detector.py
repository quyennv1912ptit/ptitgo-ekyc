import logging
from dataclasses import dataclass

import cv2
import numpy as np

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.image_loader import LoadedImage

logger = logging.getLogger(__name__)

# Tỷ lệ khung hình tiêu chuẩn của thẻ căn cước / bằng lái (ISO/IEC 7810 ID-1: 85.60mm x 53.98mm)
ID_CARD_ASPECT_RATIO = 85.60 / 53.98  # ~1.586
MIN_CARD_ASPECT_RATIO = 1.2
MAX_CARD_ASPECT_RATIO = 2.1


@dataclass(frozen=True)
class CardDetectionResult:
    """Result of card detection and perspective warping."""

    warped_image: np.ndarray
    corners: np.ndarray
    width: int
    height: int
    aspect_ratio: float


def order_points(pts: np.ndarray) -> np.ndarray:
    """Order 4 points in clockwise order: Top-Left, Top-Right, Bottom-Right, Bottom-Left."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    # Sắp vòng quanh tâm tránh trùng đỉnh khi thẻ xoay 45 độ (sum/diff bằng nhau).
    center = pts.mean(axis=0)
    angles = np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0])
    ordered = pts[np.argsort(angles)]
    start = np.argmin(ordered.sum(axis=1))
    return np.roll(ordered, -start, axis=0)


def is_card_cropped(
    corners: np.ndarray,
    img_width: int,
    img_height: int,
    margin_px: int = 8,
) -> bool:
    """Check if any corner or edge of the card touches or extends outside the image boundary."""
    for point in corners:
        x, y = point[0], point[1]
        if (
            x <= margin_px
            or x >= (img_width - margin_px)
            or y <= margin_px
            or y >= (img_height - margin_px)
        ):
            return True
    return False


def find_card_corners(
    image: np.ndarray,
    min_card_area_ratio: float = 0.15,
    max_card_area_ratio: float = 0.95,
    border_margin_px: int = 8,
) -> np.ndarray:
    """Find 4 corners of the document card in the image."""
    height, width = image.shape[:2]
    total_area = width * height
    min_area = total_area * min_card_area_ratio
    max_area = total_area * max_card_area_ratio

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # 1. Tìm cạnh bằng Canny kết hợp hình thái học
    edges = cv2.Canny(blurred, 30, 150)
    if np.count_nonzero(edges) < 50:
        logger.info("card_not_found: no distinct edges in image")
        raise EkycError(ErrorCode.CARD_NOT_FOUND)

    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        logger.info("card_not_found: no contours detected")
        raise EkycError(ErrorCode.CARD_NOT_FOUND)

    # Sắp xếp các đường bao theo diện tích giảm dần
    sorted_contours = sorted(contours, key=cv2.contourArea, reverse=True)

    best_corners: np.ndarray | None = None

    for contour in sorted_contours:
        area = cv2.contourArea(contour)
        if area > max_area:
            continue
        if area < min_area:
            break

        peri = cv2.arcLength(contour, True)
        for eps_factor in (0.015, 0.02, 0.03, 0.04, 0.05):
            approx = cv2.approxPolyDP(contour, eps_factor * peri, True)
            if len(approx) == 4 and cv2.isContourConvex(approx):
                pts = approx.reshape(4, 2)
                ordered = order_points(pts)

                # Tính tỷ lệ cạnh
                w1 = np.linalg.norm(ordered[1] - ordered[0])
                w2 = np.linalg.norm(ordered[2] - ordered[3])
                h1 = np.linalg.norm(ordered[3] - ordered[0])
                h2 = np.linalg.norm(ordered[2] - ordered[1])
                avg_w = max((w1 + w2) / 2.0, 1.0)
                avg_h = max((h1 + h2) / 2.0, 1.0)
                ratio = max(avg_w, avg_h) / min(avg_w, avg_h)

                if MIN_CARD_ASPECT_RATIO <= ratio <= MAX_CARD_ASPECT_RATIO:
                    best_corners = ordered
                    break
        if best_corners is not None:
            break

    # Nếu không tìm thấy bằng đa giác 4 đỉnh, thử hình chữ nhật xoay (MinAreaRect)
    if best_corners is None:
        for largest in sorted_contours:
            area = cv2.contourArea(largest)
            if area > max_area or area < min_area:
                continue
            rect = cv2.minAreaRect(largest)
            box = cv2.boxPoints(rect)
            ordered = order_points(box)

            w1 = np.linalg.norm(ordered[1] - ordered[0])
            w2 = np.linalg.norm(ordered[2] - ordered[3])
            h1 = np.linalg.norm(ordered[3] - ordered[0])
            h2 = np.linalg.norm(ordered[2] - ordered[1])
            avg_w = max((w1 + w2) / 2.0, 1.0)
            avg_h = max((h1 + h2) / 2.0, 1.0)
            ratio = max(avg_w, avg_h) / min(avg_w, avg_h)

            if MIN_CARD_ASPECT_RATIO <= ratio <= MAX_CARD_ASPECT_RATIO:
                best_corners = ordered
                break

    if best_corners is None:
        logger.info("card_not_found: no valid 4-corner document shape found")
        raise EkycError(ErrorCode.CARD_NOT_FOUND)

    # 2. Kiểm tra thẻ có bị xén / chạm viền ảnh không (CARD_CROPPED)
    if is_card_cropped(best_corners, width, height, margin_px=border_margin_px):
        logger.info("card_cropped: card touches image border")
        raise EkycError(ErrorCode.CARD_CROPPED)

    return best_corners


def warp_card(
    image: np.ndarray,
    corners: np.ndarray,
) -> np.ndarray:
    """Rectify and crop the detected card to a standardized flat rectangle."""
    rect = order_points(corners)
    tl, tr, br, bl = rect

    # Tính chiều rộng và chiều cao sau biến đổi
    width_a = np.linalg.norm(br - bl)
    width_b = np.linalg.norm(tr - tl)
    max_width = int(max(width_a, width_b))

    height_a = np.linalg.norm(tr - br)
    height_b = np.linalg.norm(tl - bl)
    max_height = int(max(height_a, height_b))

    if max_width <= 0 or max_height <= 0:
        raise EkycError(ErrorCode.INVALID_IMAGE)

    dst = np.array(
        [
            [0, 0],
            [max_width - 1, 0],
            [max_width - 1, max_height - 1],
            [0, max_height - 1],
        ],
        dtype=np.float32,
    )

    matrix = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(image, matrix, (max_width, max_height), flags=cv2.INTER_CUBIC)

    # Chuẩn hóa về chiều ngang (landscape) nếu ảnh bị dọc
    if warped.shape[0] > warped.shape[1]:
        warped = cv2.rotate(warped, cv2.ROTATE_90_CLOCKWISE)

    return warped


def detect_and_warp_card(
    image: np.ndarray | LoadedImage,
    min_card_area_ratio: float = 0.15,
    border_margin_px: int = 8,
) -> CardDetectionResult:
    """Full pipeline: detect 4 corners, verify not cropped, and apply warpPerspective."""
    if isinstance(image, LoadedImage):
        img = image.bgr
    elif isinstance(image, np.ndarray):
        img = image
    else:
        raise EkycError(ErrorCode.INVALID_IMAGE)

    corners = find_card_corners(
        img,
        min_card_area_ratio=min_card_area_ratio,
        border_margin_px=border_margin_px,
    )
    warped = warp_card(img, corners)

    h, w = warped.shape[:2]
    aspect_ratio = float(w / max(h, 1))

    return CardDetectionResult(
        warped_image=warped,
        corners=corners,
        width=w,
        height=h,
        aspect_ratio=aspect_ratio,
    )
