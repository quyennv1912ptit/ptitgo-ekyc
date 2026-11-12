"""Build a local YOLOv8-Pose dataset from Labelme annotations and background photos."""

import argparse
import hashlib
import json
import logging
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import albumentations as A
import cv2
import numpy as np
from sklearn.model_selection import train_test_split

ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / "Sorted_Dataset/train"
NEGATIVE_DIR = ROOT / "Sorted_Dataset/invalid_non_doc"
OUTPUT_DIR = ROOT / "card_dataset_v3"
GROUP_MAP = ROOT / "tests/fixtures/private/dataset_v3_review/groups.json"
SEED = 42
AUGMENTATIONS_PER_IMAGE = 10
AUG_MAX_SIDE = 1600
MAX_ATTEMPTS_PER_AUGMENTATION = 12
SPLITS = ("train", "val", "test")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
NEGATIVE_EXCLUSIONS = {
    # T35 contains a partly occluded card. Exclude renamed byte-identical copies too.
    "cea4d6720617b30da4651206ec57ac99c01689421aa8961287b40009d52bca15",
}
GENERIC_PREFIXES = {"img", "image", "dsc", "dscf", "photo", "pxl", "mvimg", "scan"}


@dataclass
class Photo:
    path: Path
    source: str
    digest: str
    group: str | None = None
    group_method: str | None = None
    points: np.ndarray | None = None
    size: tuple[int, int] | None = None


def ordered_corners(points):
    points = np.asarray(points, dtype=np.float32)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 4:
        raise ValueError("expected four or more xy points")
    if not np.isfinite(points).all():
        raise ValueError("non-finite coordinates")
    if len(points) > 4:
        points = cv2.boxPoints(cv2.minAreaRect(points))
    center = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - center[1], points[:, 0] - center[0])
    points = points[np.argsort(angles)]
    if not cv2.isContourConvex(points) or cv2.contourArea(points) <= 1:
        raise ValueError("corners must form a convex quadrilateral")
    # In image coordinates, ascending angles go clockwise. Break TL ties by y.
    start = np.lexsort((points[:, 1], points.sum(axis=1)))[0]
    return np.roll(points, -start, axis=0)


def decode(data):
    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("unreadable image")
    return image


def read_corners(path, width, height):
    annotation = json.loads(path.with_suffix(".json").read_text(encoding="utf-8-sig"))
    if not isinstance(annotation, dict):
        raise ValueError("expected a Labelme JSON object")
    for key, expected in (("imageWidth", width), ("imageHeight", height)):
        if annotation.get(key) not in (None, expected):
            raise ValueError(f"{key} disagrees with decoded image ({expected})")
    points = ordered_corners(annotation["shapes"][0]["points"])
    # minAreaRect can extend past an edge even when all polygon points are in frame.
    return ordered_corners(np.clip(points, (0, 0), (width, height)))


def card_group(path, source, group_map):
    if source in group_map:
        return group_map[source].casefold(), "reviewed"
    prefix, separator, _ = path.stem.partition("_")
    if separator and prefix.casefold() not in GENERIC_PREFIXES:
        return prefix.casefold(), "filename_prefix"
    return source.casefold(), "unverified_filename"


def scan(positive_dir, negative_dir, group_map, stats):
    positive_hashes = defaultdict(list)
    seen_positive_hashes = set()
    negatives = {}
    for directory, positive in ((positive_dir, True), (negative_dir, False)):
        paths = sorted(directory.rglob("*"), key=lambda path: path.as_posix().casefold())
        for path in paths:
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            source = path.relative_to(directory).as_posix()
            kind = "positive" if positive else "negative"
            stats[f"input_{kind}"] += 1
            try:
                data = path.read_bytes()
                digest = hashlib.md5(data).hexdigest()
                if positive:
                    if digest in seen_positive_hashes:
                        stats["duplicate_positive"] += 1
                    seen_positive_hashes.add(digest)
                elif digest in seen_positive_hashes or digest in negatives:
                    stats["duplicate_negative"] += 1
                    if digest in seen_positive_hashes:
                        stats["negative_matching_positive"] += 1
                        logging.warning("Negative matches a positive by MD5: %s", source)
                    continue
                if not positive and hashlib.sha256(data).hexdigest() in NEGATIVE_EXCLUSIONS:
                    stats["excluded_negative_contains_card"] += 1
                    logging.warning("Excluded negative containing a card: %s", source)
                    continue
                height, width = decode(data).shape[:2]
                photo = Photo(path, source, digest)
                if positive:
                    photo.points = read_corners(path, width, height)
                    photo.size = width, height
                    photo.group, photo.group_method = card_group(path, source, group_map)
                    positive_hashes[digest].append(photo)
                else:
                    negatives[digest] = photo
            except (OSError, ValueError, TypeError, KeyError, IndexError, cv2.error) as error:
                stats[f"invalid_{kind}"] += 1
                logging.warning("Skipping %s %s: %s", kind, source, error)

    # Equal images can bridge named groups. Merge those groups before dropping aliases.
    parents = {}

    def root(group):
        while group in parents:
            group = parents[group]
        return group

    positives = []
    for aliases in positive_hashes.values():
        aliases.sort(key=lambda photo: photo.group_method != "reviewed")
        primary = aliases[0]
        for alias in aliases[1:]:
            primary_root, alias_root = root(primary.group), root(alias.group)
            if primary_root != alias_root:
                parents[alias_root] = primary_root
        positives.append(primary)
    for photo in positives:
        photo.group = root(photo.group)
    return positives, list(negatives.values())


def split_keys(keys):
    keys = sorted(keys)
    if len(keys) < 3:
        raise ValueError(f"need at least three distinct samples or groups, got {len(keys)}")
    train, held_out = train_test_split(
        keys, test_size=max(2, int(np.ceil(len(keys) * 0.30))), random_state=SEED
    )
    val, test = train_test_split(held_out, test_size=0.5, random_state=SEED)
    return {
        key: split for split, items in zip(SPLITS, (train, val, test), strict=True) for key in items
    }


def make_augmenter():
    return A.Compose(
        [
            A.Affine(rotate=(-30, 30), scale=(0.7, 1.1), shear=(-10, 10), p=0.8),
            A.Perspective(scale=(0.02, 0.08), p=0.5),
            A.RandomBrightnessContrast(p=0.5),
            # Albumentations 2 takes a fraction of the image's maximum pixel value.
            A.GaussNoise(std_range=(3.16 / 255, 7.07 / 255), p=0.3),
            A.MotionBlur(blur_limit=5, p=0.2),
        ],
        keypoint_params=A.KeypointParams(format="xy", remove_invisible=False),
        seed=SEED,
    )


def pose_label(points, width, height):
    def clamp(value):
        return max(0.0, min(1.0, float(value)))

    xy = np.array([[clamp(x / width), clamp(y / height)] for x, y in points])
    low, high = xy.min(axis=0), xy.max(axis=0)
    values = [f"{value:.8f}" for value in (*((high + low) / 2), *(high - low))]
    for x, y in xy:
        values.extend((f"{x:.8f}", f"{y:.8f}", "2"))
    return "0 " + " ".join(values) + "\n"


def augmented_images(photo, augmenter, count, occupied_hashes):
    image = decode(photo.path.read_bytes())
    height, width = image.shape[:2]
    points = photo.points.copy()
    scale = min(1.0, AUG_MAX_SIDE / max(width, height))
    if scale < 1:
        resized = max(1, round(width * scale)), max(1, round(height * scale))
        image = cv2.resize(image, resized, interpolation=cv2.INTER_AREA)
        points *= np.array(resized) / np.array((width, height))
    saved = 0
    for _ in range(count * MAX_ATTEMPTS_PER_AUGMENTATION):
        result = augmenter(image=image, keypoints=points.tolist())
        frame = result["image"]
        height, width = frame.shape[:2]
        transformed = np.asarray(result["keypoints"])
        if transformed.shape != (4, 2) or not np.isfinite(transformed).all():
            continue
        if not ((transformed >= 0) & (transformed < (width, height))).all():
            continue
        if np.array_equal(frame, image) and np.allclose(transformed, points):
            continue
        try:
            transformed = ordered_corners(transformed)
        except ValueError:
            continue
        success, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 93])
        if not success:
            continue
        data = encoded.tobytes()
        digest = hashlib.md5(data).hexdigest()
        if digest in occupied_hashes:
            continue
        occupied_hashes.add(digest)
        saved += 1
        yield saved, data, digest, pose_label(transformed, width, height)
        if saved == count:
            return
    if saved < count:
        logging.warning("Only %d/%d valid augmentations for %s", saved, count, photo.source)


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build(input_dir, negative_dir, output_dir, group_map, augmentation_count):
    for directory in (input_dir, negative_dir):
        if not directory.is_dir():
            raise ValueError(f"source directory does not exist: {directory}")
        if output_dir.is_relative_to(directory):
            raise ValueError("output must be outside the source directories")
    if output_dir.exists():
        raise ValueError(f"output already exists: {output_dir}")
    stats = Counter()
    positives, negatives = scan(input_dir, negative_dir, group_map, stats)
    groups = split_keys({photo.group for photo in positives})
    negative_splits = split_keys(photo.digest for photo in negatives)
    logging.info(
        "Found %d CARD_ID groups across %d unique positives and %d unique negatives",
        len(groups),
        len(positives),
        len(negatives),
    )
    stats["duplicate_md5_total"] = stats["duplicate_positive"] + stats["duplicate_negative"]
    logging.info("Removed %d duplicate MD5 entries", stats["duplicate_md5_total"])

    occupied_hashes = {photo.digest for photo in positives + negatives}
    augmenter = make_augmenter()
    manifest = []
    counts = {
        split: dict(positive_original=0, positive_augmented=0, negative=0) for split in SPLITS
    }
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{output_dir.name}-", dir=output_dir.parent) as temp:
        stage = Path(temp)
        for split in SPLITS:
            (stage / "images" / split).mkdir(parents=True)
            (stage / "labels" / split).mkdir(parents=True)

        def save(photo, split, name, label, category, data=None, digest=None, parent=None):
            image_path = stage / "images" / split / name
            if data is None:
                shutil.copy2(photo.path, image_path)
            else:
                image_path.write_bytes(data)
            (stage / "labels" / split / image_path.with_suffix(".txt").name).write_text(
                label, encoding="utf-8", newline="\n"
            )
            counts[split][category] += 1
            manifest.append(
                {
                    "image": image_path.relative_to(stage).as_posix(),
                    "source": photo.source,
                    "split": split,
                    "category": category,
                    "card_id": photo.group,
                    "group_method": photo.group_method,
                    "md5": digest or photo.digest,
                    "parent": parent,
                }
            )

        for index, photo in enumerate(positives, 1):
            split = groups[photo.group]
            stem = f"p{index:05d}"
            original = stem + photo.path.suffix.lower()
            save(photo, split, original, pose_label(photo.points, *photo.size), "positive_original")
            if split == "train" and augmentation_count:
                for number, data, digest, label in augmented_images(
                    photo, augmenter, augmentation_count, occupied_hashes
                ):
                    save(
                        photo,
                        split,
                        f"{stem}_aug{number:02d}.jpg",
                        label,
                        "positive_augmented",
                        data=data,
                        digest=digest,
                        parent=original,
                    )
            if index % 25 == 0:
                logging.info("Processed %d/%d positive originals", index, len(positives))

        for index, photo in enumerate(negatives, 1):
            save(
                photo,
                negative_splits[photo.digest],
                f"n{index:05d}{photo.path.suffix.lower()}",
                "",
                "negative",
            )

        group_counts = Counter(groups.values())
        report = {
            "seed": SEED,
            "unique_card_ids": len(groups),
            "card_ids_per_split": dict(group_counts),
            "group_methods_by_positive_image": dict(
                Counter(photo.group_method for photo in positives)
            ),
            "reviewed_map_entries": len(group_map),
            "augmentation_target_per_train_positive": augmentation_count,
            "augmentation_max_side": AUG_MAX_SIDE,
            "augmentation_shortfall": counts["train"]["positive_original"] * augmentation_count
            - counts["train"]["positive_augmented"],
            "stats": dict(stats),
            "splits": counts,
        }
        (stage / "data.yaml").write_text(
            f"path: {json.dumps(output_dir.as_posix(), ensure_ascii=False)}\n"
            "train: images/train\nval: images/val\ntest: images/test\n"
            "kpt_shape: [4, 3]\nnames:\n  0: card\n",
            encoding="utf-8",
            newline="\n",
        )
        write_json(stage / "manifest.json", manifest)
        write_json(stage / "build_report.json", report)
        stage.rename(output_dir)

    for split in SPLITS:
        logging.info("%s: %d CARD_ID groups, %s", split, group_counts[split], counts[split])
    logging.info("Dataset written to %s", output_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=INPUT_DIR)
    parser.add_argument("--negative-dir", type=Path, default=NEGATIVE_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--group-map", type=Path, help="JSON object mapping relative image paths to CARD_ID"
    )
    parser.add_argument("--augmentations", type=int, default=AUGMENTATIONS_PER_IMAGE)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cv2.setNumThreads(2)
    try:
        if args.augmentations < 0:
            raise ValueError("--augmentations must be nonnegative")
        group_map = {}
        group_path = args.group_map
        if group_path is None and args.input_dir.resolve() == INPUT_DIR and GROUP_MAP.is_file():
            group_path = GROUP_MAP
        if group_path is not None:
            group_map = json.loads(group_path.read_text(encoding="utf-8-sig"))
            if not isinstance(group_map, dict) or any(
                not isinstance(key, str) or not isinstance(value, str) or not value.strip()
                for key, value in group_map.items()
            ):
                raise ValueError(
                    "group map must be a JSON object mapping image paths to nonempty strings"
                )
        build(
            args.input_dir.resolve(),
            args.negative_dir.resolve(),
            args.output_dir.resolve(),
            group_map,
            args.augmentations,
        )
    except (ValueError, OSError) as error:
        parser.exit(1, f"ERROR {error}\n")


if __name__ == "__main__":
    main()
