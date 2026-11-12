from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.card_detector import order_points
from ekyc.pipeline.card_image import DefaultCardPreparer, quality_settings
from ekyc.pipeline.geometry import GateStatus, evaluate_card_gate
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
        "ekyc.pipeline.quality.check_warped_quality",
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
    for profile in ("evaluation", "calibration"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, ekyc_api_key="test", env="prod", quality_profile=profile)


def test_quality_settings_rejects_calibration_in_prod(settings):
    config = settings.model_copy(update={"env": "prod", "quality_profile": "calibration"})
    with pytest.raises(ValueError):
        quality_settings(config)


def test_native_small_image_cannot_bypass_gate_by_resize(settings):
    # Thẻ dài 599 px trên ảnh gốc: gate chặn trước; resize về 1600 px không cứu được.
    quad = np.array([[20, 20], [619, 20], [619, 400], [20, 400]], dtype=np.float32)
    outcome = evaluate_card_gate(quad, 800, 500, settings)
    assert outcome.status is GateStatus.BLOCK
    assert outcome.error_code is ErrorCode.IMAGE_TOO_SMALL


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


def test_yolo_backend_requires_checkpoint(tmp_path):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            ekyc_api_key="test",
            card_detector_backend="yolo",
            card_yolo_model_path=str(tmp_path / "missing.pt"),
        )


def test_backend_defaults_by_checkpoint_presence(tmp_path):
    checkpoint = tmp_path / "model.pt"
    checkpoint.write_bytes(b"placeholder")
    with_checkpoint = Settings(
        _env_file=None, ekyc_api_key="test", card_yolo_model_path=str(checkpoint)
    )
    assert with_checkpoint.card_detector_backend == "yolo"
    without_checkpoint = Settings(
        _env_file=None, ekyc_api_key="test", card_yolo_model_path=str(tmp_path / "missing.pt")
    )
    assert without_checkpoint.card_detector_backend == "opencv"


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

    class Array:
        def __init__(self, value):
            self.value = np.asarray(value, dtype=np.float32)

        def cpu(self):
            return self

        def numpy(self):
            return self.value

        def __len__(self):
            return len(self.value)

    class Boxes:
        def __init__(self):
            self.xyxy = Array(np.zeros((count, 4), np.float32))
            self.conf = Array(np.zeros(count, np.float32))

        def __len__(self):
            return count

    class Keypoints:
        def __init__(self):
            self.xy = Array(np.zeros((count, 4, 2), np.float32))
            self.conf = Array(np.zeros((count, 4), np.float32))

    detector = object.__new__(YoloCardDetector)
    detector.settings, detector._lock = settings, Lock()
    detector.model = SimpleNamespace(
        predict=lambda *args, **kwargs: [SimpleNamespace(boxes=Boxes(), keypoints=Keypoints())]
    )
    with pytest.raises(EkycError) as error:
        detector.detect(image)
    assert error.value.code == ErrorCode.CARD_NOT_FOUND


def test_bbox_at_border_does_not_block_when_keypoints_inside(image, settings):
    from threading import Lock

    class Array:
        def __init__(self, value):
            self.value = np.asarray(value, dtype=np.float32)

        def __getitem__(self, item):
            return Array(self.value[item])

        def cpu(self):
            return self

        def numpy(self):
            return self.value

    class Boxes:
        def __init__(self):
            self.xyxy = Array([[100, 0, 700, 500]])
            self.conf = Array([0.9])

        def __len__(self):
            return 1

    points = [[100, 20], [700, 20], [700, 480], [100, 480]]
    keypoints = SimpleNamespace(xy=Array([points]), conf=Array([[1.0, 1.0, 1.0, 1.0]]))
    detector = object.__new__(YoloCardDetector)
    detector.settings, detector._lock = settings, Lock()
    detector.model = SimpleNamespace(
        predict=lambda *args, **kwargs: [SimpleNamespace(boxes=Boxes(), keypoints=keypoints)]
    )
    result = detector.detect(image)
    assert result.warped_image.shape[1] > result.warped_image.shape[0]
    gate = evaluate_card_gate(np.asarray(points, np.float32), 800, 600, settings)
    assert gate.status is not GateStatus.BLOCK
