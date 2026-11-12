import math
from types import SimpleNamespace

import numpy as np
import pytest

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode, WarningCode
from ekyc.pipeline.card_image import DefaultCardPreparer
from ekyc.pipeline.geometry import (
    GateStatus,
    card_native_geometry,
    evaluate_card_gate,
)


def quad(width: float, height: float, x: float = 100, y: float = 100) -> np.ndarray:
    return np.array(
        [[x, y], [x + width, y], [x + width, y + height], [x, y + height]], dtype=np.float32
    )


def rotated_quad(width: float, height: float, angle_deg: float, center=(900, 900)) -> np.ndarray:
    theta = math.radians(angle_deg)
    cos, sin = math.cos(theta), math.sin(theta)
    corners = [
        (-width / 2, -height / 2),
        (width / 2, -height / 2),
        (width / 2, height / 2),
        (-width / 2, height / 2),
    ]
    return np.array(
        [[center[0] + x * cos - y * sin, center[1] + x * sin + y * cos] for x, y in corners],
        dtype=np.float32,
    )


def test_horizontal_card_long_side() -> None:
    geometry = card_native_geometry(quad(1200, 800), 2000, 1500)
    assert geometry.long_side_px == pytest.approx(1200)
    assert geometry.short_side_px == pytest.approx(800)
    assert geometry.aspect == pytest.approx(1.5)


def test_vertical_card_long_side_is_height() -> None:
    geometry = card_native_geometry(quad(800, 1200), 1500, 2000)
    assert geometry.long_side_px == pytest.approx(1200)
    assert geometry.short_side_px == pytest.approx(800)


def test_rotated_30_degrees_card_long_side() -> None:
    geometry = card_native_geometry(rotated_quad(800, 500, 30), 1800, 1800)
    assert geometry.long_side_px == pytest.approx(800, abs=1)
    assert geometry.short_side_px == pytest.approx(500, abs=1)
    assert geometry.aspect == pytest.approx(1.6, abs=0.01)


def test_perspective_skewed_card_uses_mean_opposite_edges() -> None:
    skewed = np.array(
        [[100, 100], [900, 140], [820, 640], [160, 600]],
        dtype=np.float32,
    )
    geometry = card_native_geometry(skewed, 1200, 900)
    assert geometry.long_side_px == pytest.approx(731.1, abs=0.5)
    assert geometry.short_side_px == pytest.approx(505.0, abs=0.5)
    assert geometry.aspect == pytest.approx(1.448, abs=0.01)


def test_geometry_does_not_depend_on_keypoint_order() -> None:
    points = quad(1000, 600, 80, 70)
    reference = card_native_geometry(points, 1400, 900)
    for order in ([2, 0, 3, 1], [3, 2, 1, 0], [1, 3, 0, 2]):
        shuffled = points[order]
        result = card_native_geometry(shuffled, 1400, 900)
        assert result == reference


@pytest.mark.parametrize(
    ("long_side", "status", "error", "warning"),
    [
        (599, GateStatus.BLOCK, ErrorCode.IMAGE_TOO_SMALL, None),
        (600, GateStatus.PASS_REVIEW, None, WarningCode.LOW_RESOLUTION),
        (999, GateStatus.PASS_REVIEW, None, WarningCode.LOW_RESOLUTION),
        (1000, GateStatus.PASS, None, None),
    ],
)
def test_size_thresholds(
    settings: Settings,
    long_side: float,
    status: GateStatus,
    error: ErrorCode | None,
    warning: WarningCode | None,
) -> None:
    outcome = evaluate_card_gate(quad(long_side, long_side / 1.6, 50, 50), 1400, 900, settings)
    assert outcome.status is status
    assert outcome.error_code is error
    assert [item.code for item in outcome.warnings] == ([warning] if warning else [])


@pytest.mark.parametrize("border", ["left", "top", "right", "bottom"])
@pytest.mark.parametrize(
    ("distance", "cropped"),
    [(4, True), (5, False), (6, False)],
)
def test_keypoint_distance_to_each_border(
    settings: Settings, border: str, distance: int, cropped: bool
) -> None:
    image_w, image_h, card_w, card_h = 1100, 800, 1000, 600
    if border == "left":
        x, y = distance, 100
    elif border == "right":
        x, y = image_w - 1 - distance - card_w, 100
    elif border == "top":
        x, y = 50, distance
    else:
        x, y = 50, image_h - 1 - distance - card_h
    outcome = evaluate_card_gate(quad(card_w, card_h, x, y), image_w, image_h, settings)
    assert (outcome.error_code is ErrorCode.CARD_CROPPED) is cropped
    if not cropped:
        assert outcome.status is GateStatus.PASS


def test_keypoint_outside_image_is_cropped(settings: Settings) -> None:
    outcome = evaluate_card_gate(quad(1000, 600, -5, 100), 1100, 800, settings)
    assert outcome.status is GateStatus.BLOCK
    assert outcome.error_code is ErrorCode.CARD_CROPPED
    assert outcome.geometry.any_corner_outside is True
    assert outcome.geometry.min_border_distance_px < 0


def test_bbox_near_border_with_keypoints_inside_is_not_cropped(settings: Settings) -> None:
    # x=5, y=5 nằm trong ngưỡng cũ 8 px nhưng không dưới ngưỡng mới 5 px.
    outcome = evaluate_card_gate(quad(1000, 600, 5, 5), 1100, 800, settings)
    assert outcome.status is GateStatus.PASS
    assert outcome.error_code is None
    assert outcome.geometry.min_border_distance_px == pytest.approx(5)


def test_low_resolution_warning_message_is_vietnamese(settings: Settings) -> None:
    outcome = evaluate_card_gate(quad(800, 500, 50, 50), 1200, 800, settings)
    assert outcome.status is GateStatus.PASS_REVIEW
    assert outcome.warnings[0].message == (
        "Độ phân giải ảnh thấp, hãy kiểm tra lại các trường được trích xuất."
    )


def test_size_block_message_mentions_closer_and_full_resolution(settings: Settings) -> None:
    with pytest.raises(EkycError) as error:
        DefaultCardPreparer(settings).gate(quad(599, 374, 50, 50), 1400, 900)
    assert error.value.code == ErrorCode.IMAGE_TOO_SMALL
    assert "lại gần" in error.value.message
    assert "độ phân giải đầy đủ" in error.value.message


def test_calibration_profile_never_blocks(settings: Settings, monkeypatch) -> None:
    image = np.zeros((800, 1200, 3), dtype=np.uint8)
    outside = quad(1000, 600, -20, -20)

    def load(url: str, config: Settings) -> SimpleNamespace:
        return SimpleNamespace(bgr=image, width=1200, height=800)

    def warp(loaded: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(warped_image=image, corners=outside)

    monkeypatch.setattr("ekyc.pipeline.image_loader.load_image_from_url_sync", load)
    monkeypatch.setattr("ekyc.pipeline.card_detector.detect_and_warp_card", warp)
    monkeypatch.setattr("ekyc.pipeline.quality.check_warped_quality", lambda image, config: None)
    calibration = DefaultCardPreparer(
        settings.model_copy(update={"quality_profile": "calibration"})
    )
    prepared = calibration.prepare("unused")
    assert prepared.warnings == ()
    assert prepared.image is image
    with pytest.raises(EkycError) as error:
        DefaultCardPreparer(settings).prepare("unused")
    assert error.value.code == ErrorCode.CARD_CROPPED
