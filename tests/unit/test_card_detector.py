import cv2
import numpy as np
import pytest

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.card_detector import (
    CardDetectionResult,
    detect_and_warp_card,
    find_card_corners,
    is_card_cropped,
    order_points,
    warp_card,
)
from ekyc.pipeline.image_loader import LoadedImage


def create_card_on_background(
    img_width: int = 1200,
    img_height: int = 900,
    card_x: int = 150,
    card_y: int = 150,
    card_w: int = 800,
    card_h: int = 500,
    bg_color: int = 30,
    card_color: int = 210,
    angle_deg: float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    """Create a synthetic image of an ID card placed on a background."""
    img = np.full((img_height, img_width, 3), bg_color, dtype=np.uint8)

    # Coordinates of card rectangle before rotation
    rect_pts = np.array(
        [
            [card_x, card_y],
            [card_x + card_w, card_y],
            [card_x + card_w, card_y + card_h],
            [card_x, card_y + card_h],
        ],
        dtype=np.float32,
    )

    if angle_deg != 0.0:
        center = (card_x + card_w / 2.0, card_y + card_h / 2.0)
        m = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
        # Transform points
        ones = np.ones(shape=(len(rect_pts), 1))
        pts_ones = np.hstack([rect_pts, ones])
        rect_pts = m.dot(pts_ones.T).T

    # Draw filled polygon for card
    pts_int = np.int32(rect_pts)
    cv2.fillPoly(img, [pts_int], (card_color, card_color, card_color))

    # Add text-like patterns inside card to enrich contrast
    center_x = int(np.mean(pts_int[:, 0]))
    center_y = int(np.mean(pts_int[:, 1]))
    cv2.circle(img, (center_x, center_y), 30, (80, 80, 80), -1)

    return img, rect_pts


def test_order_points() -> None:
    # Pass points in scrambled order: TR, BL, TL, BR
    pts = np.array([[200, 50], [50, 200], [50, 50], [200, 200]], dtype=np.float32)
    ordered = order_points(pts)

    # Top-Left: [50, 50]
    np.testing.assert_array_equal(ordered[0], [50, 50])
    # Top-Right: [200, 50]
    np.testing.assert_array_equal(ordered[1], [200, 50])
    # Bottom-Right: [200, 200]
    np.testing.assert_array_equal(ordered[2], [200, 200])
    # Bottom-Left: [50, 200]
    np.testing.assert_array_equal(ordered[3], [50, 200])


def test_is_card_cropped() -> None:
    # Card well inside image (1200x900)
    inside_corners = np.array([[100, 100], [800, 100], [800, 500], [100, 500]])
    assert not is_card_cropped(inside_corners, img_width=1200, img_height=900, margin_px=8)

    # Corner touching left border (x = 3 <= 8)
    cropped_left = np.array([[3, 100], [800, 100], [800, 500], [3, 500]])
    assert is_card_cropped(cropped_left, img_width=1200, img_height=900, margin_px=8)

    # Corner touching bottom border (y = 896 >= 900 - 8)
    cropped_bottom = np.array([[100, 100], [800, 100], [800, 896], [100, 896]])
    assert is_card_cropped(cropped_bottom, img_width=1200, img_height=900, margin_px=8)


def test_card_cropped_error() -> None:
    # Card touching the left edge (card_x = 2 <= 8)
    img, _ = create_card_on_background(img_width=1200, img_height=900, card_x=2, card_y=100)
    with pytest.raises(EkycError) as exc_info:
        find_card_corners(img, min_card_area_ratio=0.15, border_margin_px=8)
    assert exc_info.value.code == ErrorCode.CARD_CROPPED


def test_card_not_found_solid_background() -> None:
    # Solid background with no card
    solid_img = np.full((900, 1200, 3), 128, dtype=np.uint8)
    with pytest.raises(EkycError) as exc_info:
        find_card_corners(solid_img, min_card_area_ratio=0.15)
    assert exc_info.value.code == ErrorCode.CARD_NOT_FOUND


def test_card_not_found_tiny_shape() -> None:
    # Card area is too small (< 15% of total area)
    # Total area: 1200x900 = 1,080,000. Card: 100x100 = 10,000 (< 1%)
    img, _ = create_card_on_background(
        img_width=1200, img_height=900, card_x=100, card_y=100, card_w=100, card_h=100
    )
    with pytest.raises(EkycError) as exc_info:
        find_card_corners(img, min_card_area_ratio=0.15)
    assert exc_info.value.code == ErrorCode.CARD_NOT_FOUND


def test_detect_and_warp_card_success() -> None:
    # Card 800x500 (~1.6 ratio) well within 1200x900 frame
    img, _ = create_card_on_background(
        img_width=1200, img_height=900, card_x=200, card_y=150, card_w=800, card_h=500
    )
    result = detect_and_warp_card(img, min_card_area_ratio=0.15, border_margin_px=8)

    assert isinstance(result, CardDetectionResult)
    assert result.corners.shape == (4, 2)
    # Warped image should have aspect ratio near 1.6
    assert 1.4 <= result.aspect_ratio <= 1.8
    assert result.warped_image.shape[1] > result.warped_image.shape[0]  # Landscape


def test_detect_and_warp_card_rotated() -> None:
    # Card placed at a slight 5 degree tilt
    img, _ = create_card_on_background(
        img_width=1200,
        img_height=900,
        card_x=200,
        card_y=150,
        card_w=750,
        card_h=470,
        angle_deg=5.0,
    )
    result = detect_and_warp_card(img, min_card_area_ratio=0.15, border_margin_px=8)
    assert result.corners.shape == (4, 2)
    assert 1.4 <= result.aspect_ratio <= 1.8


def test_detect_and_warp_loaded_image() -> None:
    img, _ = create_card_on_background(
        img_width=1200, img_height=900, card_x=200, card_y=150, card_w=800, card_h=500
    )
    loaded = LoadedImage(
        bgr=img,
        rgb=cv2.cvtColor(img, cv2.COLOR_BGR2RGB),
        width=1200,
        height=900,
        raw_bytes=b"dummy",
    )
    result = detect_and_warp_card(loaded)
    assert result.width > result.height


def test_warp_card_direct() -> None:
    # Direct warp with 4 points
    img = np.full((600, 800, 3), 100, dtype=np.uint8)
    corners = np.array([[100, 100], [500, 100], [500, 350], [100, 350]], dtype=np.float32)
    warped = warp_card(img, corners)

    assert warped.shape[1] == 400
    assert warped.shape[0] == 250
