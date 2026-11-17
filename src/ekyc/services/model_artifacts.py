"""Verify the pinned staging inference stack without importing models or downloading files."""

import argparse
import hashlib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from ekyc.config import Settings


@dataclass(frozen=True)
class Artifact:
    release: str
    sha256: str


CARD_MODEL = Artifact(
    "card-pose-v3-best",
    "ea75a70de1cab77d0ffbe1d56109f2d7046ea059f1fbfa3fe32f270cb23d377e",
)
EASYOCR_MODELS = {
    "craft_mlt_25k.pth": Artifact(
        "easyocr-1.7.2-craft",
        "4a5efbfb48b4081100544e75e1e2b57f8de3d84f213004b14b85fd4b3748db17",
    ),
    "latin_g2.pth": Artifact(
        "easyocr-1.7.2-latin-g2",
        "aaa95be1c4a9cb3496879bed7c520886ce1164f89e026f0c54488394e74e8c55",
    ),
}
PACKAGES = {
    "easyocr": "1.7.2",
    "ultralytics-opencv-headless": "8.4.208",
    "opencv-python-headless": "5.0.0.93",
    "torch": "2.15.0",
    "torchvision": "0.29.2",
}


class ModelIntegrityError(Exception):
    def __init__(self, code: str, artifact: str):
        self.code = code
        self.artifact = artifact
        super().__init__(code)


def ocr_directory(settings: Settings) -> Path:
    if settings.easyocr_model_dir is not None:
        return settings.easyocr_model_dir
    return Path.home() / ".EasyOCR" / "model"


def verify_file(path: Path, artifact: Artifact) -> None:
    try:
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        raise ModelIntegrityError("MODEL_FILE_MISSING", artifact.release) from None
    if actual != artifact.sha256:
        raise ModelIntegrityError("MODEL_HASH_MISMATCH", artifact.release)


def verify_packages() -> None:
    for package, expected in PACKAGES.items():
        try:
            actual = version(package).split("+", maxsplit=1)[0]
        except PackageNotFoundError:
            raise ModelIntegrityError("MODEL_DEPENDENCY_MISSING", package) from None
        if actual != expected:
            raise ModelIntegrityError("MODEL_DEPENDENCY_MISMATCH", package)
    for package in ("opencv-python", "opencv-contrib-python", "opencv-contrib-python-headless"):
        try:
            version(package)
        except PackageNotFoundError:
            continue
        raise ModelIntegrityError("MODEL_DEPENDENCY_MISMATCH", package)


def verify_models(settings: Settings) -> None:
    if settings.ocr_backend != "easyocr" or settings.card_detector_backend != "yolo":
        raise ModelIntegrityError("MODEL_CONFIG_UNSUPPORTED", "inference_profile")
    if set(settings.ocr_languages) != {"vi", "en"} or len(settings.ocr_languages) != 2:
        raise ModelIntegrityError("MODEL_CONFIG_UNSUPPORTED", "ocr_languages")
    verify_packages()
    verify_file(Path(settings.card_yolo_model_path), CARD_MODEL)
    for filename, artifact in EASYOCR_MODELS.items():
        verify_file(ocr_directory(settings) / filename, artifact)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packages-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.packages_only:
            verify_packages()
        else:
            verify_models(Settings())
    except ModelIntegrityError as exc:
        print(f"{exc.code} artifact={exc.artifact}")
        return 1
    except ValueError:
        print("MODEL_CONFIG_UNSUPPORTED")
        return 1
    print("Inference integrity checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
