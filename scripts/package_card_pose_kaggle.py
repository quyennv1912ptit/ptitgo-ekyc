"""Package only card labels/images, minimal train code and a seed model for private Kaggle use."""

import argparse
import hashlib
import json
import zipfile
from collections import Counter
from pathlib import Path

import numpy as np
import yaml

CODE_FILES = [
    "scripts/train_card_detector.py",
    "scripts/select_card_checkpoint.py",
    "src/ekyc/__init__.py",
    "src/ekyc/config.py",
    "src/ekyc/core/__init__.py",
    "src/ekyc/core/errors.py",
    "src/ekyc/pipeline/__init__.py",
    "src/ekyc/pipeline/card_detector.py",
    "src/ekyc/pipeline/image_loader.py",
    "src/ekyc/pipeline/yolo_card_detector.py",
]


def package_dataset(dataset: Path, seed: Path, output: Path, root: Path) -> dict:
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}; choose a new ZIP path")
    if not seed.is_file():
        raise FileNotFoundError(seed)
    groups = json.loads((dataset / "manifest_summary.json").read_text(encoding="utf-8"))
    source_yaml = yaml.safe_load((dataset / "data.yaml").read_text(encoding="utf-8"))
    if source_yaml.get("names") != {0: "card"} or source_yaml.get("kpt_shape") != [4, 3]:
        raise ValueError("Expected one card class and four keypoints")
    files: dict[str, Path] = {name: root / name for name in CODE_FILES}
    files["models/seed.pt"] = seed
    counts: Counter = Counter()
    ids: set[str] = set()
    for split in ("train", "val"):
        images = sorted((dataset / "images" / split).glob("*.jpg"))
        labels = set((dataset / "labels" / split).glob("*.txt"))
        expected_labels = set()
        for image in images:
            if image.stem in ids or not image.stem.isalnum():
                raise ValueError("Duplicate or unsafe image ID across splits")
            ids.add(image.stem)
            label = dataset / "labels" / split / f"{image.stem}.txt"
            expected_labels.add(label)
            values = np.loadtxt(label).reshape(-1)
            if values.shape != (17,) or not np.isfinite(values).all() or values[0] != 0:
                raise ValueError(f"Invalid pose label: {image.stem}")
            points = values[5:].reshape(4, 3)
            if (
                np.any(values[1:5] < 0)
                or np.any(values[1:5] > 1)
                or np.any(points[:, :2] < 0)
                or np.any(points[:, :2] > 1)
                or not np.isin(points[:, 2], [1, 2]).all()
            ):
                raise ValueError(f"Invalid normalized coordinates: {image.stem}")
            files[f"dataset/images/{split}/{image.name}"] = image
            files[f"dataset/labels/{split}/{label.name}"] = label
            counts[split] += 1
        if labels != expected_labels or not images or counts[split] != groups["counts"][split]:
            raise ValueError("Dataset files/counts differ from exported group manifest")
    portable_yaml = {
        "path": ".",
        "train": "images/train",
        "val": "images/val",
        "names": {0: "card"},
        "kpt_shape": [4, 3],
    }
    if "kpt_oks_sigmas" in source_yaml:
        sigmas = np.asarray(source_yaml["kpt_oks_sigmas"], dtype=float)
        if sigmas.shape != (4,) or not np.isfinite(sigmas).all() or np.any(sigmas <= 0):
            raise ValueError("Expected four positive keypoint sigmas")
        portable_yaml["kpt_oks_sigmas"] = sigmas.tolist()
    payloads = {name: path.read_bytes() for name, path in files.items()}
    payloads["dataset/data.yaml"] = yaml.safe_dump(portable_yaml).encode("utf-8")
    payloads["dataset/group_summary.json"] = json.dumps(groups, indent=2).encode("utf-8")
    manifest = {
        "kind": "ekyc-card-pose-kaggle-v1",
        "counts": dict(counts),
        "ultralytics": "8.4.208",
        "label_quality": "approximate; review corners before training",
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in payloads.items():
            archive.writestr(f"card_pose_kaggle/{name}", data)
        archive.writestr("card_pose_kaggle/bundle_manifest.json", json.dumps(manifest, indent=2))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset", type=Path, default=Path("tests/fixtures/private/card_pose_dataset/export")
    )
    parser.add_argument("--seed", type=Path, default=Path("models/card_yolov8n_pose.selected.pt"))
    parser.add_argument(
        "--output", type=Path, default=Path("tests/fixtures/private/card_pose_kaggle.zip")
    )
    args = parser.parse_args()
    manifest = package_dataset(args.dataset, args.seed, args.output, Path.cwd())
    print(json.dumps({"output": str(args.output), "counts": manifest["counts"]}))
    print("Contains private card images. Upload only to a private dataset/notebook.")


if __name__ == "__main__":
    main()
