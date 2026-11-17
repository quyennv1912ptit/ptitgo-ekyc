from dataclasses import replace

import numpy as np
import pytest

from ekyc.core.errors import EkycError, ErrorCode, WarningCode
from ekyc.pipeline.geometry import GateStatus
from ekyc.pipeline.keypoint_recovery import prepare_pose
from ekyc.pipeline.yolo_card_detector import CardDetection


@pytest.fixture
def pose():
    return CardDetection(
        np.array([[100, 100], [900, 100], [900, 600], [100, 600]], np.float32),
        np.array([90, 90, 910, 610], np.float32),
        0.9,
        0,
        np.array([0.95, 0.2, 0.95, 0.95], np.float32),
    )


@pytest.fixture
def frame():
    return np.zeros((700, 1000, 3), np.uint8)


def test_low_confidence_is_review_only(settings, pose, frame):
    with pytest.raises(EkycError) as error:
        prepare_pose(frame, pose, settings)
    assert error.value.details == [{"reason": "KEYPOINT_LOW_CONFIDENCE"}]
    result = prepare_pose(
        frame, pose, settings.model_copy(update={"card_experimental_keypoint_recovery": True})
    )
    assert result.status is GateStatus.PASS_REVIEW
    assert WarningCode.LOW_KEYPOINT_CONFIDENCE in {warning.code for warning in result.warnings}
    assert result.edge_support == 0  # Diagnostic, even a geometrically plausible blank is review.


def test_high_confidence_inset_quad_cannot_certify_full_card(settings, pose, frame):
    # A coherent inward shift can conceal a true edge violation from the predicted-quad gate.
    from ekyc.pipeline.geometry import evaluate_card_gate

    reference = pose.quad_native.copy()
    reference[[0, 3], 0] = 2
    assert evaluate_card_gate(reference, 1000, 700, settings).error_code is ErrorCode.CARD_CROPPED
    prediction = replace(pose, keypoint_confidence=np.full(4, 0.99, np.float32))
    result = prepare_pose(frame, prediction, settings)
    assert result.status is GateStatus.PASS_REVIEW  # Only the existing low-resolution warning.
    assert {warning.code for warning in result.warnings} == {WarningCode.LOW_RESOLUTION}


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("two_weak", "KEYPOINT_LOW_CONFIDENCE"),
        ("inside_box", "KEYPOINT_LOW_CONFIDENCE"),
        ("crossed", "KEYPOINT_INVALID_GEOMETRY"),
        ("nan", "KEYPOINT_INVALID_GEOMETRY"),
        ("inf", "KEYPOINT_INVALID_GEOMETRY"),
        ("missing", "KEYPOINT_INVALID_GEOMETRY"),
        ("border", "CARD_CROPPED"),
        ("outside", "CARD_CROPPED"),
        ("small", "IMAGE_TOO_SMALL"),
        ("warp", "WARP_FAILED"),
    ],
)
def test_recovery_rejection(settings, pose, frame, monkeypatch, scenario, reason):
    def fail_warp(*args):
        raise EkycError(ErrorCode.INVALID_IMAGE)

    monkeypatch.setattr("ekyc.pipeline.keypoint_recovery.rectify_corners", fail_warp)
    quad = pose.quad_native.copy()
    if scenario == "two_weak":
        pose.keypoint_confidence[:2] = 0.01
    elif scenario == "inside_box":
        pose.box_native[:] = [0, 0, 999, 699]
    elif scenario == "crossed":
        quad = quad[[0, 2, 1, 3]]
    elif scenario in ("nan", "inf"):
        quad[0, 0] = float(scenario)
    elif scenario == "missing":
        pose = replace(pose, keypoint_confidence=None)
    elif scenario in ("border", "outside"):
        quad[[0, 3], 0] = 0 if scenario == "border" else -1
    elif scenario == "small":
        quad = quad / 2
    with pytest.raises(EkycError) as error:
        prepare_pose(
            frame,
            replace(pose, quad_native=quad),
            settings.model_copy(update={"card_experimental_keypoint_recovery": True}),
        )
    assert error.value.details == [{"reason": reason}]
    if scenario in ("border", "outside"):
        assert error.value.code is ErrorCode.CARD_CROPPED
