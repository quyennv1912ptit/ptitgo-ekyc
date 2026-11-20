"""Experimental assisted face comparison with explicit per-submission consent."""

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Protocol

import cv2
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from ekyc.config import Settings
from ekyc.core.errors import EkycError
from ekyc.pipeline.image_loader import load_image_from_bytes

CONSENT_VERSION = "face-comparison-v1"
PAIRS = (("cccd_front", "selfie"), ("gplx_front", "selfie"), ("cccd_front", "gplx_front"))
SLOTS = ("cccd_front", "gplx_front", "selfie")
PairStatus = Literal[
    "NOT_EVALUATED",
    "INSUFFICIENT_QUALITY",
    "INCONCLUSIVE",
    "POSSIBLE_MISMATCH",
    "SIMILARITY_SUPPORTED",
]


class PairResult(BaseModel):
    left: str
    right: str
    status: PairStatus
    score: float | None = None


class FaceResult(BaseModel):
    mode: Literal["NOT_EVALUATED", "EXPERIMENTAL"] = "NOT_EVALUATED"
    reason: str | None = None
    modelVersion: str | None = None
    qualityVersion: str | None = None
    thresholdVersion: str | None = None
    scoreMetric: Literal["COSINE_SIMILARITY"] | None = None
    liveness: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"
    quality: dict[str, str] = Field(default_factory=dict)
    pairs: list[PairResult] = Field(
        default_factory=lambda: [
            PairResult(left=left, right=right, status="NOT_EVALUATED") for left, right in PAIRS
        ]
    )


class QualityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,48}$")
    analysis_max_side: int = Field(default=512, ge=64, le=2048)
    min_face_pixels: int = Field(ge=2, le=512)
    min_blur_score: float = Field(gt=0)
    max_glare_fraction: float = Field(gt=0, lt=1)
    min_brightness: float = Field(gt=0, lt=255)


class ModelManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detector: str
    detector_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    recognizer: str
    recognizer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    detector_max_side: int = Field(ge=64, le=2048)
    detector_min_score: float = Field(gt=0, lt=1)
    provenance_reference: str = Field(min_length=1, max_length=200)
    quality: QualityProfile


class FaceEngine(Protocol):
    version: str
    quality: QualityProfile

    def detect(self, image: np.ndarray) -> list[np.ndarray]: ...
    def feature(self, image: np.ndarray, region: np.ndarray) -> np.ndarray: ...


def model_file(directory: Path, name: str, digest: str) -> Path:
    path = (directory / name).resolve()
    if not path.is_relative_to(directory) or path.suffix.lower() != ".onnx":
        raise ValueError("Invalid local face model path")
    with path.open("rb") as model:
        if hashlib.file_digest(model, "sha256").hexdigest() != digest:
            raise ValueError("Face model checksum mismatch")
    return path


class OpenCvFaceEngine:
    """OpenCV YuNet and SFace. Loads pinned files from a local directory only."""

    def __init__(self, manifest_path: Path):
        directory = manifest_path.resolve().parent
        if str(directory).startswith(("//", "\\\\")):
            raise ValueError("Face models require a local directory")
        manifest = ModelManifest.model_validate_json(manifest_path.read_bytes())
        detector = model_file(directory, manifest.detector, manifest.detector_sha256)
        recognizer = model_file(directory, manifest.recognizer, manifest.recognizer_sha256)
        self.detector = cv2.FaceDetectorYN.create(
            str(detector), "", (320, 320), manifest.detector_min_score
        )
        self.recognizer = cv2.FaceRecognizerSF.create(str(recognizer), "")
        self.max_side = manifest.detector_max_side
        self.quality = manifest.quality
        self.version = (
            f"opencv-{cv2.__version__}:"
            f"yunet-{manifest.detector_sha256[:12]}:sface-{manifest.recognizer_sha256[:12]}"
        )

    def detect(self, image: np.ndarray) -> list[np.ndarray]:
        height, width = image.shape[:2]
        scale = min(1.0, self.max_side / max(width, height))
        if scale < 1:
            view = cv2.resize(
                image,
                (max(1, round(width * scale)), max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        else:
            view = image
        h, w = view.shape[:2]
        self.detector.setInputSize((w, h))
        _, detected = self.detector.detect(view)
        if detected is None:
            return []
        # Convert coordinates back to source resolution before measuring or aligning.
        faces = detected.copy()
        faces[:, :14:2] *= width / w
        faces[:, 1:14:2] *= height / h
        return list(faces)

    def feature(self, image: np.ndarray, region: np.ndarray) -> np.ndarray:
        return self.recognizer.feature(self.recognizer.alignCrop(image, region)).copy()


def enabled(settings: Settings) -> bool:
    return bool(
        settings.face_comparison_enabled
        and settings.face_comparison_manifest
        and settings.face_comparison_manifest.is_file()
        and settings.ocr_backend == "easyocr"
    )


def inspect_face(image: np.ndarray, face: np.ndarray, policy: QualityProfile) -> str:
    if face.shape != (15,) or not np.isfinite(face).all():
        return "FACE_LANDMARKS_UNRELIABLE"
    x, y, width, height = face[:4]
    if min(width, height) < policy.min_face_pixels:
        return "FACE_TOO_SMALL"
    if x < 0 or y < 0 or x + width > image.shape[1] or y + height > image.shape[0]:
        return "FACE_CROPPED"
    points = face[4:14].reshape(5, 2)
    if (
        len(np.unique(points, axis=0)) != 5
        or np.any(points < [x, y])
        or np.any(points > [x + width, y + height])
    ):
        return "FACE_LANDMARKS_UNRELIABLE"
    crop = image[int(y) : int(y + height), int(x) : int(x + width)]
    factor = min(1.0, policy.analysis_max_side / max(crop.shape[:2]))
    if factor < 1:
        crop = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if (gray >= 250).mean() > policy.max_glare_fraction:
        return "FACE_GLARE"
    if gray.mean() < policy.min_brightness:
        return "FACE_TOO_DARK"
    if cv2.Laplacian(gray, cv2.CV_64F).var() < policy.min_blur_score:
        return "FACE_BLURRY"
    return "READY"


class FaceComparison:
    def __init__(self, settings: Settings, prepare_card: Callable, engine: FaceEngine):
        self.settings = settings
        self.prepare_card = prepare_card
        self.engine = engine

    def extract(self, raw: bytes, slot: str, check: Callable[[], None], *, quality_only=False):
        try:
            loaded = load_image_from_bytes(raw, self.settings)
        except EkycError:
            return "INVALID_IMAGE", None
        check()
        image = loaded.bgr
        if slot != "selfie":
            try:
                image = self.prepare_card(loaded).image
            except (EkycError, cv2.error):
                return "CARD_PREPARATION_FAILED", None
        check()
        faces = self.engine.detect(image)
        if not faces and slot != "selfie":
            image = cv2.rotate(image, cv2.ROTATE_180)
            faces = self.engine.detect(image)
        if not faces:
            return "NO_FACE", None
        if len(faces) != 1:
            return "MULTIPLE_FACES", None
        status = inspect_face(image, faces[0], self.engine.quality)
        if quality_only or status != "READY":
            return status, None
        check()
        try:
            vector = np.asarray(self.engine.feature(image, faces[0]), dtype=np.float64).reshape(-1)
        except (cv2.error, TypeError, ValueError):
            return "FACE_ALIGNMENT_FAILED", None
        magnitude = np.linalg.norm(vector)
        if not np.isfinite(magnitude) or magnitude == 0:
            return "FACE_LANDMARKS_UNRELIABLE", None
        return "READY", vector / magnitude

    def evaluate(
        self, images: dict[str, bytes], check: Callable[[], None], *, quality_only=False
    ) -> FaceResult:
        quality, features = {}, {}
        for slot in SLOTS:
            check()
            if slot not in images:
                quality[slot] = "MISSING_IMAGE"
                continue
            quality[slot], features[slot] = self.extract(
                images[slot], slot, check, quality_only=quality_only
            )
        if quality_only:
            reason = "QUALITY_ONLY" if any(slot in images for slot in SLOTS) else "MISSING_IMAGE"
            return FaceResult(reason=reason, quality=quality)
        result = FaceResult(
            mode="EXPERIMENTAL",
            reason="INSUFFICIENT_EVIDENCE",
            modelVersion=self.engine.version,
            qualityVersion=self.engine.quality.version,
            scoreMetric="COSINE_SIMILARITY",
            quality=quality,
            pairs=[],
        )
        for left, right in PAIRS:
            if "MISSING_IMAGE" in (quality[left], quality[right]):
                result.pairs.append(PairResult(left=left, right=right, status="NOT_EVALUATED"))
            elif quality[left] != "READY" or quality[right] != "READY":
                result.pairs.append(
                    PairResult(left=left, right=right, status="INSUFFICIENT_QUALITY")
                )
            else:
                a, b = features[left], features[right]
                score = float(np.clip(np.dot(a, b), -1, 1)) if a.shape == b.shape else None
                result.pairs.append(
                    PairResult(
                        left=left,
                        right=right,
                        status="INCONCLUSIVE",
                        score=round(score, 6) if score is not None else None,
                    )
                )
        return result
