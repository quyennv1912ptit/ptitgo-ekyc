"""Export a reviewed JSON corner manifest to a local YOLO pose dataset."""

import argparse
import json
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageOps


def export_dataset(manifest: Path, output: Path, root: Path) -> dict:
    if output.exists() and any(output.iterdir()):
        raise FileExistsError(
            "Use an empty output directory to avoid stale labels or split leakage"
        )
    rows = json.loads(manifest.read_text(encoding="utf-8"))
    groups: dict[str, str] = {}
    counts: Counter = Counter()
    ids: set[str] = set()
    for row in rows:
        if not row.get("include", True):
            continue
        split, group, identifier = row["split"], row["group"], row["id"]
        if split not in {"train", "val"} or not identifier.isalnum() or identifier in ids:
            raise ValueError("Invalid split or duplicate/unsafe image ID")
        ids.add(identifier)
        if group in groups and groups[group] != split:
            raise ValueError(f"Group leakage: {group}")
        groups[group] = split
        points = np.asarray(row["corners"], dtype=np.float32)
        visibility = row.get("visibility", [2] * 4)
        if (
            points.shape != (4, 2)
            or not np.isfinite(points).all()
            or np.any(points < 0)
            or np.any(points > 1)
            or not cv2.isContourConvex(points)
            or cv2.contourArea(points) < 0.01
            or len(visibility) != 4
            or any(v not in {1, 2} for v in visibility)
        ):
            raise ValueError(f"Invalid corner annotation: {identifier}")
        image_dir, label_dir = output / "images" / split, output / "labels" / split
        image_dir.mkdir(parents=True, exist_ok=True)
        label_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(root / row["path"]) as original:
            image = ImageOps.exif_transpose(original).convert("RGB")
            if list(image.size) != row["size"]:
                raise ValueError(f"Image dimensions changed: {identifier}")
            image.thumbnail((1600, 1600))
            image.save(image_dir / f"{identifier}.jpg", quality=95)
            preview = image.copy()
            pixels = [tuple(map(float, p)) for p in points * preview.size]
            ImageDraw.Draw(preview).line([*pixels, pixels[0]], fill="red", width=4)
            review_dir = output / "annotation_review"
            review_dir.mkdir(exist_ok=True)
            preview.save(review_dir / f"{identifier}.jpg", quality=90)
        low, high = points.min(axis=0), points.max(axis=0)
        center, size = (low + high) / 2, high - low
        label = [0, *center, *size]
        for point, visible in zip(points, visibility, strict=True):
            label.extend([*point, visible])
        (label_dir / f"{identifier}.txt").write_text(
            " ".join(f"{float(v):.7f}" for v in label) + "\n", encoding="utf-8"
        )
        counts[split] += 1
    if not counts["train"] or not counts["val"]:
        raise ValueError("Both train and validation splits are required")
    data = {
        "path": output.resolve().as_posix(),
        "train": "images/train",
        "val": "images/val",
        "names": {0: "card"},
        "kpt_shape": [4, 3],
    }
    (output / "data.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    report = {"counts": dict(counts), "groups": groups, "experimental": True}
    (output / "manifest_summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(export_dataset(args.manifest, args.output, args.root)["counts"]))


if __name__ == "__main__":
    main()
