from threading import Lock

import numpy as np
import pytest

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.yolo_card_detector import (
    DetectionStatus,
    YoloCardDetector,
    canonicalize,
    is_valid_detection,
    map_box,
    map_point,
    rotate_frame,
)


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
    def __init__(self, boxes, confidence):
        self.xyxy = Array(boxes)
        self.conf = Array(confidence)

    def __len__(self):
        return len(self.conf.value)


class Keypoints:
    def __init__(self, points, confidence):
        self.xy = Array(points)
        self.conf = Array(confidence)


def result(boxes=(), confidence=(), points=(), keypoint_confidence=()):
    from types import SimpleNamespace

    boxes = np.asarray(boxes, np.float32).reshape(-1, 4)
    points = np.asarray(points, np.float32).reshape(-1, 4, 2)
    keypoint_confidence = np.asarray(keypoint_confidence, np.float32).reshape(-1, 4)
    return SimpleNamespace(
        boxes=Boxes(boxes, np.asarray(confidence, np.float32).reshape(-1)),
        keypoints=Keypoints(points, keypoint_confidence),
    )


def card(box=(100, 100, 900, 600), confidence=0.9, offset=(0, 0)):
    x1, y1, x2, y2 = box
    dx, dy = offset
    points = [
        [x1 + dx, y1 + dy],
        [x1 + dx, y2 + dy],
        [x2 + dx, y2 + dy],
        [x2 + dx, y1 + dy],
    ]
    return result([box], [confidence], [points], [[1, 1, 1, 1]])


def rotated_card(box=(50, 50, 600, 450), confidence=0.9):
    return card(box=box, confidence=confidence)


EMPTY = result()


def detector(settings: Settings, results: list) -> YoloCardDetector:
    class FakeModel:
        def __init__(self, items):
            self.items = list(items)
            self.calls = []

        def predict(self, image, **kwargs):
            self.calls.append(kwargs["conf"])
            return [self.items.pop(0)]

    fake = object.__new__(YoloCardDetector)
    fake.settings = settings
    fake._lock = Lock()
    fake.model = FakeModel(results)
    return fake


def test_detection_at_zero_degrees(settings: Settings) -> None:
    outcome = detector(settings, [card()]).detect_cards(np.zeros((700, 1000, 3), np.uint8))
    assert outcome.status is DetectionStatus.DETECTED
    assert outcome.rotation == 0
    assert outcome.passes_run == 1
    assert outcome.detection is not None


def test_empty_at_zero_falls_back_to_cw(settings: Settings) -> None:
    outcome = detector(settings, [EMPTY, rotated_card()]).detect_cards(
        np.zeros((700, 1000, 3), np.uint8)
    )
    assert outcome.rotation == 90
    assert outcome.passes_run == 2
    assert outcome.detection is not None
    assert outcome.detection.quad_native[:, 0].max() <= 999
    assert outcome.detection.quad_native[:, 1].max() <= 699
    assert outcome.detection.quad_native.min() >= 0


def test_empty_at_cw_falls_back_to_ccw(settings: Settings) -> None:
    outcome = detector(settings, [EMPTY, EMPTY, rotated_card()]).detect_cards(
        np.zeros((700, 1000, 3), np.uint8)
    )
    assert outcome.rotation == -90
    assert outcome.passes_run == 3


def test_all_passes_empty(settings: Settings) -> None:
    outcome = detector(settings, [EMPTY, EMPTY, EMPTY]).detect_cards(
        np.zeros((700, 1000, 3), np.uint8)
    )
    assert outcome.status is DetectionStatus.NOT_FOUND
    assert outcome.error_code is ErrorCode.CARD_NOT_FOUND
    assert outcome.passes_run == 3


def test_invalid_geometry_triggers_fallback(settings: Settings) -> None:
    square = card(box=(100, 100, 600, 600))
    outcome = detector(settings, [square, rotated_card()]).detect_cards(
        np.zeros((700, 1000, 3), np.uint8)
    )
    assert outcome.rotation == 90
    assert outcome.passes_run == 2


def test_duplicate_boxes_are_merged(settings: Settings) -> None:
    outcome = detector(
        settings,
        [
            result(
                [[100, 100, 900, 600], [102, 101, 901, 601]],
                [0.9, 0.8],
                [
                    [[100, 100], [900, 100], [900, 600], [100, 600]],
                    [[102, 101], [901, 101], [901, 601], [102, 601]],
                ],
                [[1, 1, 1, 1], [1, 1, 1, 1]],
            )
        ],
    ).detect_cards(np.zeros((700, 1000, 3), np.uint8))
    assert outcome.status is DetectionStatus.DETECTED
    assert outcome.ignored_detections == 0


def test_contained_box_is_merged(settings: Settings) -> None:
    outcome = detector(
        settings,
        [
            result(
                [[100, 100, 900, 600], [300, 250, 500, 400]],
                [0.9, 0.8],
                [
                    [[100, 100], [900, 100], [900, 600], [100, 600]],
                    [[300, 250], [500, 250], [500, 400], [300, 400]],
                ],
                [[1, 1, 1, 1], [1, 1, 1, 1]],
            )
        ],
    ).detect_cards(np.zeros((700, 1000, 3), np.uint8))
    assert outcome.status is DetectionStatus.DETECTED
    assert outcome.ignored_detections == 0


def test_weak_secondary_is_ignored(settings: Settings) -> None:
    outcome = detector(
        settings,
        [
            result(
                [[100, 100, 900, 600], [900, 620, 990, 680]],
                [0.9, 0.4],
                [
                    [[100, 100], [900, 100], [900, 600], [100, 600]],
                    [[900, 620], [990, 620], [990, 680], [900, 680]],
                ],
                [[1, 1, 1, 1], [1, 1, 1, 1]],
            )
        ],
    ).detect_cards(np.zeros((700, 1000, 3), np.uint8))
    assert outcome.status is DetectionStatus.DETECTED
    assert outcome.ignored_detections == 1


def test_strong_secondary_gives_multiple_cards(settings: Settings) -> None:
    outcome = detector(
        settings,
        [
            result(
                [[100, 100, 600, 500], [650, 100, 950, 300]],
                [0.9, 0.9],
                [
                    [[100, 100], [600, 100], [600, 500], [100, 500]],
                    [[650, 100], [950, 100], [950, 300], [650, 300]],
                ],
                [[1, 1, 1, 1], [1, 1, 1, 1]],
            )
        ],
    ).detect_cards(np.zeros((700, 1000, 3), np.uint8))
    assert outcome.status is DetectionStatus.MULTIPLE_CARDS
    assert outcome.error_code is ErrorCode.MULTIPLE_CARDS
    assert outcome.detection is None


def test_overlapping_secondary_is_ambiguous(settings: Settings) -> None:
    outcome = detector(
        settings,
        [
            result(
                [[100, 100, 800, 600], [600, 300, 900, 500]],
                [0.9, 0.9],
                [
                    [[100, 100], [800, 100], [800, 600], [100, 600]],
                    [[600, 300], [900, 300], [900, 500], [600, 500]],
                ],
                [[1, 1, 1, 1], [1, 1, 1, 1]],
            )
        ],
    ).detect_cards(np.zeros((700, 1000, 3), np.uint8))
    assert outcome.status is DetectionStatus.DETECTED
    assert outcome.ignored_detections == 1


def test_detect_raises_multiple_cards(settings: Settings) -> None:
    fake = detector(
        settings,
        [
            result(
                [[100, 100, 600, 500], [650, 100, 950, 300]],
                [0.9, 0.9],
                [
                    [[100, 100], [600, 100], [600, 500], [100, 500]],
                    [[650, 100], [950, 100], [950, 300], [650, 300]],
                ],
                [[1, 1, 1, 1], [1, 1, 1, 1]],
            )
        ],
    )
    with pytest.raises(EkycError) as error:
        fake.detect(np.zeros((700, 1000, 3), np.uint8))
    assert error.value.code == ErrorCode.MULTIPLE_CARDS
    assert "chụp riêng" in error.value.message


@pytest.mark.parametrize("rotation", [0, 90, -90])
@pytest.mark.parametrize(("x", "y"), [(0, 0), (3, 2), (10, 6)])
def test_marker_round_trip(rotation: int, x: int, y: int) -> None:
    image = np.zeros((7, 11, 3), np.uint8)
    image[y, x] = 255
    rotated = rotate_frame(image, rotation)
    rows, columns = np.where(rotated[:, :, 0] == 255)
    mapped = map_point(float(columns[0]), float(rows[0]), rotation, 11, 7)
    assert mapped == (float(x), float(y))


@pytest.mark.parametrize("rotation", [90, -90])
def test_box_mapping_uses_four_corners(rotation: int) -> None:
    mapped = map_box(np.array([1, 2, 5, 8], np.float32), rotation, 11, 7)
    assert mapped.shape == (4,)
    assert mapped[0] < mapped[2] and mapped[1] < mapped[3]


def test_validity_rejects_bad_aspect(settings: Settings) -> None:
    square = canonicalize(
        np.array([[0, 0], [100, 0], [100, 100], [0, 100]], np.float32),
        np.ones(4),
        np.array([0, 0, 100, 100], np.float32),
        0.9,
        0,
        1000,
        800,
    )
    assert not is_valid_detection(square, 0.25, settings)
