from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.card_detector import order_points
from ekyc.pipeline.card_image import DefaultCardPreparer, quality_settings
from ekyc.pipeline.yolo_card_detector import YoloCardDetector, rectify_corners


@pytest.fixture
def image():
    return np.zeros((600, 800, 3), dtype=np.uint8)


@pytest.fixture
def corners():
    return np.array([[100, 100], [700, 120], [680, 490], [120, 480]], dtype=np.float32)


def test_perspective_and_portrait(image, corners, settings):
    result = rectify_corners(image, corners, np.ones(4), settings)
    assert result.width > result.height
    assert 1.4 < result.aspect_ratio < 1.8
    portrait = np.rot90(image).copy()
    rotated = np.column_stack([corners[:, 1], 799 - corners[:, 0]]).astype(np.float32)
    result = rectify_corners(portrait, rotated, np.ones(4), settings)
    assert result.width > result.height


def test_diamond_has_four_distinct_ordered_corners():
    points = np.array([[100, 0], [200, 100], [100, 200], [0, 100]], dtype=np.float32)
    assert len(np.unique(order_points(points), axis=0)) == 4


@pytest.mark.parametrize("bad", ["duplicate", "nan", "tiny", "square", "confidence", "outside"])
def test_reject_bad_corners(image, corners, settings, bad):
    confidence = np.ones(4)
    if bad == "duplicate":
        corners[1] = corners[0]
    elif bad == "nan":
        corners[1, 0] = np.nan
    elif bad == "tiny":
        corners /= 20
    elif bad == "square":
        corners = np.array([[100, 100], [500, 100], [500, 500], [100, 500]], np.float32)
    elif bad == "confidence":
        confidence[2] = 0.1
    else:
        corners[0, 0] = -2
    with pytest.raises(EkycError) as error:
        rectify_corners(image, corners, confidence, settings)
    expected = ErrorCode.CARD_CROPPED if bad == "outside" else ErrorCode.CARD_NOT_FOUND
    assert error.value.code == expected


def test_quality_runs_on_native_landscape_before_resize(settings, monkeypatch):
    observed = []
    monkeypatch.setattr(
        "ekyc.pipeline.quality.check_image_quality",
        lambda image, config: observed.append(image.shape),
    )
    result = DefaultCardPreparer(settings).finish(np.zeros((400, 250, 3), np.uint8))
    assert observed == [(250, 400, 3)]
    assert result.shape == (1009, 1600, 3)


def test_evaluation_quality_profile(settings):
    assert quality_settings(settings) is settings
    evaluation = quality_settings(settings.model_copy(update={"quality_profile": "evaluation"}))
    assert evaluation.blur_threshold == 20
    assert evaluation.glare_ratio_max == 0.15
    assert evaluation.min_image_side_px == 320
    assert settings.blur_threshold == 100
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ekyc_api_key="test", env="prod", quality_profile="evaluation")


def test_native_small_image_cannot_bypass_gate_by_resize(settings):
    with pytest.raises(EkycError) as error:
        DefaultCardPreparer(settings).finish(np.full((400, 700, 3), 128, np.uint8))
    assert error.value.code == ErrorCode.IMAGE_TOO_SMALL


def test_evaluation_still_rejects_severe_blur(settings):
    config = settings.model_copy(update={"quality_profile": "evaluation"})
    with pytest.raises(EkycError) as error:
        DefaultCardPreparer(config).finish(np.full((500, 800, 3), 128, np.uint8))
    assert error.value.code == ErrorCode.IMAGE_BLURRY


def test_missing_checkpoint_does_not_fallback(settings, tmp_path):
    config = settings.model_copy(
        update={
            "card_detector_backend": "yolo",
            "card_yolo_model_path": str(tmp_path / "missing.pt"),
        }
    )
    with pytest.raises(FileNotFoundError):
        DefaultCardPreparer(config)


@pytest.mark.parametrize(
    ("task", "shape", "names"),
    [
        ("detect", [4, 3], {0: "card"}),
        ("pose", [17, 3], {0: "card"}),
        ("pose", [4, 3], {0: "person"}),
    ],
)
def test_incompatible_checkpoint_rejected(settings, tmp_path, monkeypatch, task, shape, names):
    import sys

    path = tmp_path / "model.pt"
    path.write_bytes(b"test placeholder")
    model = SimpleNamespace(
        task=task, model=SimpleNamespace(yaml={"kpt_shape": shape}), names=names
    )
    monkeypatch.setitem(
        sys.modules,
        "ultralytics",
        SimpleNamespace(
            YOLO=lambda *args, **kwargs: model,
            settings=SimpleNamespace(update=lambda values: None),
        ),
    )
    monkeypatch.setenv("YOLO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("YOLO_OFFLINE", "true")
    with pytest.raises(ValueError):
        YoloCardDetector(settings.model_copy(update={"card_yolo_model_path": str(path)}))


@pytest.mark.parametrize("count", [0, 2])
def test_no_or_multiple_cards_rejected(image, settings, count):
    from threading import Lock

    detector = object.__new__(YoloCardDetector)
    detector.settings, detector._lock = settings, Lock()
    detector.model = SimpleNamespace(
        predict=lambda *args, **kwargs: [SimpleNamespace(boxes=[0] * count)]
    )
    with pytest.raises(EkycError) as error:
        detector.detect(image)
    assert error.value.code == ErrorCode.CARD_NOT_FOUND


def test_bbox_at_border_rejected_before_inferred_corners(image, settings):
    from threading import Lock

    class Boxes:
        xyxy = [
            SimpleNamespace(
                cpu=lambda: SimpleNamespace(numpy=lambda: np.array([100, 0, 700, 500], np.float32))
            )
        ]

        def __len__(self):
            return 1

    detector = object.__new__(YoloCardDetector)
    detector.settings, detector._lock = settings, Lock()
    detector.model = SimpleNamespace(
        predict=lambda *args, **kwargs: [SimpleNamespace(boxes=Boxes())]
    )
    with pytest.raises(EkycError) as error:
        detector.detect(image)
    assert error.value.code == ErrorCode.CARD_CROPPED
