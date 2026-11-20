from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from ekyc.services.face_comparison import FaceComparison, QualityProfile, enabled, inspect_face

PROFILE = QualityProfile(
    version="synthetic-q",
    min_face_pixels=20,
    min_blur_score=30,
    max_glare_fraction=0.2,
    min_brightness=30,
)
FACE = np.float32([20, 20, 60, 60, 35, 40, 65, 40, 50, 50, 40, 65, 60, 65, 0.9])


def checkerboard():
    y, x = np.indices((100, 100))
    return np.uint8(np.repeat(np.where((x + y) % 2, 170, 90)[:, :, None], 3, axis=2))


def encoded(image):
    success, data = cv2.imencode(".png", image)
    assert success
    return data.tobytes()


class FakeEngine:
    version = "synthetic-engine"
    quality = PROFILE

    def __init__(self, detections=None):
        self.detections = [FACE] if detections is None else detections
        self.calls = 0

    def detect(self, image):
        return self.detections

    def feature(self, image, face):
        self.calls += 1
        return np.ones(2)


def compare(settings, engine, images):
    service = FaceComparison(settings, lambda loaded: SimpleNamespace(image=loaded.bgr), engine)
    return service.evaluate(images, lambda: None)


def test_all_three_pairs_remain_inconclusive_even_at_maximum_similarity(settings):
    raw = encoded(checkerboard())
    engine = FakeEngine()
    result = compare(settings, engine, dict.fromkeys(("cccd_front", "gplx_front", "selfie"), raw))
    assert engine.calls == 3
    assert {(pair.left, pair.right) for pair in result.pairs} == {
        ("cccd_front", "selfie"),
        ("gplx_front", "selfie"),
        ("cccd_front", "gplx_front"),
    }
    assert {pair.status for pair in result.pairs} == {"INCONCLUSIVE"}
    assert {pair.score for pair in result.pairs} == {1}
    assert result.liveness == "NOT_EVALUATED" and result.thresholdVersion is None
    assert "embedding" not in result.model_dump_json()


@pytest.mark.parametrize(
    ("detections", "quality"), [([], "NO_FACE"), ([FACE, FACE], "MULTIPLE_FACES")]
)
def test_rejects_missing_and_multiple_faces_without_extracting_features(
    settings, detections, quality
):
    raw = encoded(checkerboard())
    engine = FakeEngine(detections)
    result = compare(settings, engine, {"cccd_front": raw, "selfie": raw})
    assert result.quality == {
        "cccd_front": quality,
        "gplx_front": "MISSING_IMAGE",
        "selfie": quality,
    }
    assert result.pairs[0].status == "INSUFFICIENT_QUALITY"
    assert result.pairs[1].status == "NOT_EVALUATED" and engine.calls == 0


def test_image_quality_warnings_use_native_pixels_and_avoid_guessing_occlusion():
    image = checkerboard()
    small = FACE.copy()
    small[2:4] = 5
    clipped = FACE.copy()
    clipped[0] = -3
    landmark = FACE.copy()
    landmark[4:6] = 0
    assert inspect_face(image, small, PROFILE) == "FACE_TOO_SMALL"
    assert inspect_face(image, clipped, PROFILE) == "FACE_CROPPED"
    assert inspect_face(image, landmark, PROFILE) == "FACE_LANDMARKS_UNRELIABLE"
    assert inspect_face(np.full_like(image, 255), FACE, PROFILE) == "FACE_GLARE"
    assert inspect_face(np.full_like(image, 100), FACE, PROFILE) == "FACE_BLURRY"


def test_runtime_requires_opt_in_and_manifest_in_all_environments(settings, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.touch()
    assert not enabled(settings)
    config = settings.model_copy(
        update={
            "face_comparison_enabled": True,
            "face_comparison_manifest": manifest,
            "ocr_backend": "easyocr",
        }
    )
    assert enabled(config)
    assert enabled(config.model_copy(update={"env": "prod"}))
    assert enabled(config.model_copy(update={"env": "staging"}))
    assert not enabled(
        config.model_copy(update={"face_comparison_manifest": tmp_path / "missing.json"})
    )


def test_model_loader_rejects_bad_hash_and_outside_directory(tmp_path):
    from ekyc.services.face_comparison import model_file

    model = tmp_path / "weights.onnx"
    model.write_bytes(b"local synthetic")
    with pytest.raises(ValueError, match="checksum"):
        model_file(tmp_path, model.name, "0" * 64)
    with pytest.raises(ValueError, match="path"):
        model_file(tmp_path, "../outside.onnx", "0" * 64)


def test_draft_quality_uses_detector_only_and_cannot_generate_similarity(settings):
    raw = encoded(checkerboard())
    engine = FakeEngine()
    service = FaceComparison(settings, lambda loaded: SimpleNamespace(image=loaded.bgr), engine)
    result = service.evaluate({"cccd_front": raw, "selfie": raw}, lambda: None, quality_only=True)
    assert result.reason == "QUALITY_ONLY"
    assert result.quality["selfie"] == "READY"
    assert engine.calls == 0
    assert service.evaluate({}, lambda: None, quality_only=True).reason == "MISSING_IMAGE"
    assert all(pair.status == "NOT_EVALUATED" and pair.score is None for pair in result.pairs)


def test_inconsistent_embedding_dimensions_abstain_without_raising(settings):
    class FakeVariableEngine(FakeEngine):
        def feature(self, image, face):
            self.calls += 1
            return np.ones(self.calls)

    raw = encoded(checkerboard())
    result = compare(settings, FakeVariableEngine(), {"cccd_front": raw, "selfie": raw})
    assert result.pairs[0].status == "INCONCLUSIVE"
    assert result.pairs[0].score is None
