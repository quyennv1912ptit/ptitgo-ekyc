import cv2
import numpy as np
import pytest

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.image_loader import LoadedImage
from ekyc.pipeline.quality import (
    calculate_blur_score,
    calculate_brightness,
    calculate_glare_ratio,
    check_image_quality,
    check_image_size,
    check_warped_quality,
    inspect_image_quality,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        ekyc_api_key="test-key",
        min_image_side_px=1000,
        min_brightness=40.0,
        blur_threshold=100.0,
        glare_ratio_max=0.04,
    )


def create_sharp_image(width: int = 1200, height: int = 1000, base_val: int = 128) -> np.ndarray:
    """Create a high-contrast sharp image with strong edges without saturated highlights."""
    img = np.full((height, width, 3), base_val, dtype=np.uint8)
    # Add contrast grid lines (no pixels >= 250, so glare_ratio is 0.0)
    for x in range(0, width, 20):
        cv2.line(img, (x, 0), (x, height), (base_val - 40, base_val - 40, base_val - 40), 2)
    for y in range(0, height, 20):
        cv2.line(img, (0, y), (width, y), (base_val + 40, base_val + 40, base_val + 40), 2)
    return img


def test_image_too_small(settings: Settings) -> None:
    # 800x1200 -> min_side is 800 < 1000
    with pytest.raises(EkycError) as exc_info:
        check_image_size(width=1200, height=800, min_side_px=settings.min_image_side_px)
    assert exc_info.value.code == ErrorCode.IMAGE_TOO_SMALL

    # 1000x1500 -> min_side is 1000 >= 1000 -> passes
    assert check_image_size(width=1500, height=1000, min_side_px=settings.min_image_side_px) == 1000


def test_image_too_dark(settings: Settings) -> None:
    # Very dark image: mean intensity 20 < 40
    dark_img = create_sharp_image(width=1200, height=1000, base_val=15)
    brightness = calculate_brightness(dark_img)
    assert brightness < settings.min_brightness

    with pytest.raises(EkycError) as exc_info:
        check_image_quality(dark_img, settings)
    assert exc_info.value.code == ErrorCode.IMAGE_TOO_DARK


def test_image_glare(settings: Settings) -> None:
    # Create sharp image, then introduce overexposed glare region in 10% of image
    img = create_sharp_image(width=1200, height=1000, base_val=120)
    # 10% area set to 255 (glare)
    h, w = img.shape[:2]
    img[: int(h * 0.35), : int(w * 0.35)] = 255

    glare_ratio = calculate_glare_ratio(img)
    assert glare_ratio > settings.glare_ratio_max

    with pytest.raises(EkycError) as exc_info:
        check_image_quality(img, settings)
    assert exc_info.value.code == ErrorCode.IMAGE_GLARE


def test_image_blurry(settings: Settings) -> None:
    # Solid gray image without edges has blur_score near 0
    solid_img = np.full((1000, 1200, 3), 128, dtype=np.uint8)
    blur_score = calculate_blur_score(solid_img)
    assert blur_score < settings.blur_threshold

    with pytest.raises(EkycError) as exc_info:
        check_image_quality(solid_img, settings)
    assert exc_info.value.code == ErrorCode.IMAGE_BLURRY

    # Heavily blurred image using large Gaussian blur
    sharp_img = create_sharp_image(width=1200, height=1000)
    blurred_img = cv2.GaussianBlur(sharp_img, (51, 51), 0)
    assert calculate_blur_score(blurred_img) < settings.blur_threshold

    with pytest.raises(EkycError) as exc_info:
        check_image_quality(blurred_img, settings)
    assert exc_info.value.code == ErrorCode.IMAGE_BLURRY


def test_check_image_quality_success(settings: Settings) -> None:
    img = create_sharp_image(width=1200, height=1000, base_val=128)
    metrics = check_image_quality(img, settings)

    assert metrics.width == 1200
    assert metrics.height == 1000
    assert metrics.min_side_px == 1000
    assert metrics.brightness >= settings.min_brightness
    assert metrics.glare_ratio <= settings.glare_ratio_max
    assert metrics.blur_score >= settings.blur_threshold


def test_check_image_quality_loaded_image(settings: Settings) -> None:
    img = create_sharp_image(width=1200, height=1000, base_val=128)
    loaded = LoadedImage(
        bgr=img,
        rgb=cv2.cvtColor(img, cv2.COLOR_BGR2RGB),
        width=1200,
        height=1000,
        raw_bytes=b"dummy",
    )
    metrics = check_image_quality(loaded, settings)
    assert metrics.min_side_px == 1000


def test_inspect_image_quality_multiple_errors(settings: Settings) -> None:
    # Tiny (300x200), dark (val=10), blurry (solid)
    bad_img = np.full((200, 300, 3), 10, dtype=np.uint8)
    metrics, errors = inspect_image_quality(bad_img, settings)

    assert metrics.min_side_px == 200
    assert metrics.brightness < settings.min_brightness
    assert metrics.blur_score < settings.blur_threshold
    assert ErrorCode.IMAGE_TOO_SMALL in errors
    assert ErrorCode.IMAGE_TOO_DARK in errors
    assert ErrorCode.IMAGE_BLURRY in errors


def test_warped_quality_ignores_size(settings: Settings) -> None:
    small_but_sharp = create_sharp_image(width=120, height=80)
    metrics = check_warped_quality(small_but_sharp, settings)
    assert metrics.min_side_px == 80


def test_warped_quality_still_rejects_blur(settings: Settings) -> None:
    with pytest.raises(EkycError) as exc_info:
        check_warped_quality(np.full((400, 700, 3), 128, dtype=np.uint8), settings)
    assert exc_info.value.code == ErrorCode.IMAGE_BLURRY
