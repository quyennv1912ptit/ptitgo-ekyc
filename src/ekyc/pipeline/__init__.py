"""Các tiện ích ảnh được import khi dùng, độc lập với classifier OCR."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "CardDetectionResult": "ekyc.pipeline.card_detector",
    "detect_and_warp_card": "ekyc.pipeline.card_detector",
    "find_card_corners": "ekyc.pipeline.card_detector",
    "is_card_cropped": "ekyc.pipeline.card_detector",
    "order_points": "ekyc.pipeline.card_detector",
    "warp_card": "ekyc.pipeline.card_detector",
    "LoadedImage": "ekyc.pipeline.image_loader",
    "decode_and_transpose_image": "ekyc.pipeline.image_loader",
    "fetch_image_bytes": "ekyc.pipeline.image_loader",
    "fetch_image_bytes_sync": "ekyc.pipeline.image_loader",
    "load_image_from_bytes": "ekyc.pipeline.image_loader",
    "load_image_from_url": "ekyc.pipeline.image_loader",
    "load_image_from_url_sync": "ekyc.pipeline.image_loader",
    "validate_image_url": "ekyc.pipeline.image_loader",
    "QualityMetrics": "ekyc.pipeline.quality",
    "calculate_blur_score": "ekyc.pipeline.quality",
    "calculate_brightness": "ekyc.pipeline.quality",
    "calculate_glare_ratio": "ekyc.pipeline.quality",
    "check_image_quality": "ekyc.pipeline.quality",
    "inspect_image_quality": "ekyc.pipeline.quality",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(name)
    return getattr(import_module(_EXPORTS[name]), name)
