"""Pre-deploy checks for Modal entrypoint, pinned manifests, and read-only volume contents."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

from ekyc.services.face_comparison import ModelManifest
from ekyc.services.model_artifacts import CARD_MODEL, EASYOCR_MODELS, PACKAGES

ROOT = Path(__file__).resolve().parents[1]
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FACE_ARTIFACTS = {
    "face_detection_yunet_2023mar.onnx": (
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
    ),
    "face_recognition_sface_2021dec.onnx": (
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
    ),
}
REQUIRED_VOLUME_FILES = (
    "models/card_pose_v3_best.pt",
    "ocr/craft_mlt_25k.pth",
    "ocr/latin_g2.pth",
)


def verify_manifest_constants() -> dict[str, object]:
    artifacts = {
        "models/card_pose_v3_best.pt": CARD_MODEL.sha256,
        **{f"ocr/{name}": item.sha256 for name, item in EASYOCR_MODELS.items()},
    }
    for path, digest in artifacts.items():
        if not SHA256_RE.fullmatch(digest):
            raise ValueError(f"Invalid SHA-256 digest for {path}")
    for name, digest in FACE_ARTIFACTS.items():
        if not SHA256_RE.fullmatch(digest):
            raise ValueError(f"Invalid SHA-256 digest for {name}")
    if not PACKAGES:
        raise ValueError("Missing pinned inference packages")
    return {
        "inference_artifacts": sorted(artifacts),
        "face_artifacts": sorted(FACE_ARTIFACTS),
        "packages": dict(PACKAGES),
    }


def load_modal_module(path: Path | None = None):
    module_path = path or (ROOT / "scripts" / "modal_ekyc.py")
    spec = importlib.util.spec_from_file_location("modal_ekyc_check", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Modal module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_modal_entrypoint(
    *, expected_app_name: str = "ekyc-extraction-staging"
) -> dict[str, str]:
    import modal

    module = load_modal_module()
    if module.app.name != expected_app_name:
        raise ValueError(
            f"Modal app name mismatch: expected {expected_app_name!r}, got {module.app.name!r}"
        )
    if not isinstance(getattr(module, "cccd_api", None), modal.Function):
        raise ValueError("Modal endpoint 'cccd_api' is not registered on the app")
    return {
        "app_name": module.app.name,
        "endpoint": "cccd_api",
        "model_source": module.MODEL_SOURCE,
        "env": module.resolve_app_env(),
    }


def _entry_map(entries) -> dict[str, int]:
    result: dict[str, int] = {}
    for entry in entries:
        key = entry.path.lstrip("/")
        result[key] = int(getattr(entry, "size", 0))
    return result


def verify_volume_contents(volume) -> dict[str, object]:
    files: dict[str, int] = {}
    for prefix in ("models", "ocr"):
        try:
            entries = volume.listdir(prefix)
        except Exception as exc:  # pragma: no cover - network/Modal API error path
            raise RuntimeError(
                f"Missing required Modal Volume directory '/{prefix}': {exc}"
            ) from exc
        files.update(_entry_map(entries))

    missing = [path for path in REQUIRED_VOLUME_FILES if files.get(path, 0) <= 0]
    if missing:
        raise RuntimeError(
            "Modal Volume is missing required non-empty weights: " + ", ".join(missing)
        )

    face_verified = False
    try:
        face_entries = _entry_map(volume.listdir("face"))
    except Exception:
        face_entries = {}
    if "face/manifest.json" in face_entries:
        raw_manifest = b"".join(volume.read_file("face/manifest.json"))
        manifest = ModelManifest.model_validate_json(raw_manifest)
        expected_det = FACE_ARTIFACTS.get(manifest.detector)
        expected_rec = FACE_ARTIFACTS.get(manifest.recognizer)
        if expected_det and manifest.detector_sha256 != expected_det:
            raise ValueError("Face detector SHA-256 mismatch in Modal Volume manifest")
        if expected_rec and manifest.recognizer_sha256 != expected_rec:
            raise ValueError("Face recognizer SHA-256 mismatch in Modal Volume manifest")
        for filename in (manifest.detector, manifest.recognizer):
            if face_entries.get(f"face/{filename}", 0) <= 0:
                raise RuntimeError(f"Missing face ONNX model in Modal Volume: face/{filename}")
        face_verified = True

    return {
        "verified_files": list(REQUIRED_VOLUME_FILES),
        "face_manifest_verified": face_verified,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Run manifest and entrypoint checks")
    parser.add_argument(
        "--check-volume",
        action="store_true",
        help="Verify the configured Modal Volume exists and contains required model files",
    )
    parser.add_argument("--expected-app", default="ekyc-extraction-staging")
    args = parser.parse_args(argv)

    summary: dict[str, object] = {}
    if args.offline or not args.check_volume:
        summary["manifests"] = verify_manifest_constants()
        summary["entrypoint"] = verify_modal_entrypoint(expected_app_name=args.expected_app)

    if args.check_volume:
        import modal

        volume_name = (
            os.environ.get("MODAL_MODEL_VOLUME_NAME", "ekyc-models").strip() or "ekyc-models"
        )
        env_name = os.environ.get("MODAL_ENVIRONMENT", "").strip() or None
        volume = modal.Volume.from_name(
            volume_name, environment_name=env_name, create_if_missing=False
        )
        summary["volume"] = {"name": volume_name, **verify_volume_contents(volume)}

    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
