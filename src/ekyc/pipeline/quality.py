import logging
from dataclasses import dataclass

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.image_loader import LoadedImage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QualityMetrics:
    """Metrics evaluated during image quality inspection."""

    width: int
    height: int
    min_side_px: int
    brightness: float
    glare_ratio: float
    blur_score: float


def to_gray(image: np.ndarray) -> np.ndarray:
    """Convert input image array to grayscale if needed."""
    if len(image.shape) == 2:
        return image
    if len(image.shape) == 3:
        if image.shape[2] == 3:
            return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        if image.shape[2] == 4:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        if image.shape[2] == 1:
            return image[:, :, 0]
    raise EkycError(ErrorCode.INVALID_IMAGE)


def check_image_size(width: int, height: int, min_side_px: int) -> int:
    """Check that the minimum side of the image meets the resolution threshold."""
    min_side = min(width, height)
    if min_side < min_side_px:
        raise EkycError(ErrorCode.IMAGE_TOO_SMALL)
    return min_side


def calculate_brightness(image_or_gray: np.ndarray) -> float:
    """Calculate mean pixel brightness of the image (range 0 to 255)."""
    gray = to_gray(image_or_gray)
    if gray.size == 0:
        return 0.0
    return float(np.mean(gray))


def calculate_glare_ratio(image_or_gray: np.ndarray, glare_threshold: int = 250) -> float:
    """Calculate the ratio of overexposed/glare pixels (intensity >= glare_threshold)."""
    gray = to_gray(image_or_gray)
    if gray.size == 0:
        return 0.0
    glare_pixels = int(np.count_nonzero(gray >= glare_threshold))
    return float(glare_pixels / gray.size)


def calculate_blur_score(image_or_gray: np.ndarray) -> float:
    """Calculate blur metric using the variance of the Laplacian (higher means sharper)."""
    gray = to_gray(image_or_gray)
    if gray.size == 0:
        return 0.0
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def inspect_image_quality(
    image: np.ndarray | LoadedImage,
    settings: Settings,
) -> tuple[QualityMetrics, list[ErrorCode]]:
    """Compute all quality metrics without raising exceptions, returning metrics and errors."""
    if isinstance(image, LoadedImage):
        img = image.bgr
        width, height = image.width, image.height
    elif isinstance(image, np.ndarray):
        img = image
        if img.ndim < 2:
            raise EkycError(ErrorCode.INVALID_IMAGE)
        height, width = img.shape[:2]
    else:
        raise EkycError(ErrorCode.INVALID_IMAGE)

    gray = to_gray(img)
    min_side = min(width, height)
    brightness = calculate_brightness(gray)
    glare_ratio = calculate_glare_ratio(gray)
    blur_score = calculate_blur_score(gray)

    metrics = QualityMetrics(
        width=width,
        height=height,
        min_side_px=min_side,
        brightness=brightness,
        glare_ratio=glare_ratio,
        blur_score=blur_score,
    )

    errors: list[ErrorCode] = []
    if min_side < settings.min_image_side_px:
        errors.append(ErrorCode.IMAGE_TOO_SMALL)
    if brightness < settings.min_brightness:
        errors.append(ErrorCode.IMAGE_TOO_DARK)
    if glare_ratio > settings.glare_ratio_max:
        errors.append(ErrorCode.IMAGE_GLARE)
    if blur_score < settings.blur_threshold:
        errors.append(ErrorCode.IMAGE_BLURRY)

    return metrics, errors


def check_image_quality(
    image: np.ndarray | LoadedImage,
    settings: Settings,
) -> QualityMetrics:
    """Validate image quality (size, dark, glare, blur) and raise EkycError on failure."""
    if isinstance(image, LoadedImage):
        img = image.bgr
        width, height = image.width, image.height
    elif isinstance(image, np.ndarray):
        img = image
        if img.ndim < 2:
            raise EkycError(ErrorCode.INVALID_IMAGE)
        height, width = img.shape[:2]
    else:
        raise EkycError(ErrorCode.INVALID_IMAGE)

    # 1. Kiểm tra kích thước cạnh tối thiểu (IMAGE_TOO_SMALL)
    min_side = check_image_size(width, height, settings.min_image_side_px)

    gray = to_gray(img)

    # 2. Kiểm tra độ sáng (IMAGE_TOO_DARK)
    brightness = calculate_brightness(gray)
    if brightness < settings.min_brightness:
        logger.info("image_too_dark: brightness=%.2f < %.2f", brightness, settings.min_brightness)
        raise EkycError(ErrorCode.IMAGE_TOO_DARK)

    # 3. Kiểm tra độ lóa (IMAGE_GLARE)
    glare_ratio = calculate_glare_ratio(gray)
    if glare_ratio > settings.glare_ratio_max:
        logger.info("image_glare: ratio=%.4f > %.4f", glare_ratio, settings.glare_ratio_max)
        raise EkycError(ErrorCode.IMAGE_GLARE)

    # 4. Kiểm tra độ mờ (IMAGE_BLURRY)
    blur_score = calculate_blur_score(gray)
    if blur_score < settings.blur_threshold:
        logger.info("image_blurry: score=%.2f < %.2f", blur_score, settings.blur_threshold)
        raise EkycError(ErrorCode.IMAGE_BLURRY)

    return QualityMetrics(
        width=width,
        height=height,
        min_side_px=min_side,
        brightness=brightness,
        glare_ratio=glare_ratio,
        blur_score=blur_score,
    )


# Crop đã nắn không chặn theo kích thước: gate hình học trên ảnh gốc đã quyết định.
WARPED_MIN_SIDE_PX = 1


def check_warped_quality(image: np.ndarray | LoadedImage, settings: Settings) -> QualityMetrics:
    """Kiểm tra blur, glare, độ sáng trên crop đã nắn, bỏ qua ngưỡng kích thước."""
    metrics, errors = inspect_image_quality(
        image, settings.model_copy(update={"min_image_side_px": WARPED_MIN_SIDE_PX})
    )
    for code in (ErrorCode.IMAGE_TOO_DARK, ErrorCode.IMAGE_GLARE, ErrorCode.IMAGE_BLURRY):
        if code in errors:
            raise EkycError(code)
    return metrics
