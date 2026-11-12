from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.geometry import (
    IMAGE_TOO_SMALL_MESSAGE,
    GateStatus,
    evaluate_card_gate,
)
from ekyc.schemas.common import Warning


@dataclass(frozen=True)
class PreparedCard:
    """Ảnh thẻ đã chuẩn hóa kèm cảnh báo phát sinh ở tầng chuẩn bị ảnh."""

    image: np.ndarray
    warnings: tuple[Warning, ...] = ()


class CardImagePreparer(Protocol):
    def prepare(self, url: str) -> PreparedCard: ...


class DefaultCardPreparer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.detector = None
        if settings.card_detector_backend == "yolo":
            from ekyc.pipeline.yolo_card_detector import YoloCardDetector

            self.detector = YoloCardDetector(settings)

    def prepare(self, url: str) -> PreparedCard:
        try:
            from ekyc.pipeline.card_detector import detect_and_warp_card
            from ekyc.pipeline.image_loader import load_image_from_url_sync
        except ModuleNotFoundError as exc:
            if exc.name in {
                "ekyc.pipeline.image_loader",
                "ekyc.pipeline.quality",
                "ekyc.pipeline.card_detector",
            }:
                raise NotImplementedError("TODO bước 2") from exc
            raise
        loaded = load_image_from_url_sync(url, self.settings)
        result = (
            self.detector.detect(loaded.bgr)
            if self.detector is not None
            else detect_and_warp_card(loaded)
        )
        warnings: tuple[Warning, ...] = ()
        if self.settings.quality_profile != "calibration":
            warnings = self.gate(result.corners, loaded.width, loaded.height)
        return PreparedCard(self.finish(result.warped_image), warnings)

    def gate(self, quad: np.ndarray, width: int, height: int) -> tuple[Warning, ...]:
        """Gate hình học trên ảnh gốc; BLOCK ném lỗi, PASS_REVIEW trả cảnh báo."""
        outcome = evaluate_card_gate(quad, width, height, self.settings)
        if outcome.status is GateStatus.BLOCK:
            if outcome.error_code is ErrorCode.IMAGE_TOO_SMALL:
                raise EkycError(ErrorCode.IMAGE_TOO_SMALL, IMAGE_TOO_SMALL_MESSAGE)
            raise EkycError(outcome.error_code or ErrorCode.CARD_NOT_FOUND)
        return outcome.warnings

    def finish(self, image: np.ndarray) -> np.ndarray:
        # Xoay hình dọc sang ngang; hướng chữ 180 độ vẫn do luồng OCR hiện tại xử lý.
        if image.shape[0] > image.shape[1]:
            image = cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        from ekyc.pipeline.quality import check_warped_quality

        check_warped_quality(image, quality_settings(self.settings))
        if self.settings.quality_profile == "calibration":
            return image
        return cv2.resize(
            image,
            (self.settings.card_output_width, self.settings.card_output_height),
            interpolation=cv2.INTER_AREA,
        )


def quality_settings(settings: Settings) -> Settings:
    if settings.quality_profile == "strict":
        return settings
    if settings.env == "prod":
        if settings.quality_profile == "calibration":
            raise ValueError("Calibration quality profile must not run in prod")
        raise ValueError("Evaluation quality profile must not run in prod")
    if settings.quality_profile == "calibration":
        return settings.model_copy(
            update={
                "min_image_side_px": 1,
                "min_brightness": 0,
                "glare_ratio_max": 1,
                "blur_threshold": 0,
            }
        )
    return settings.model_copy(
        update={
            "blur_threshold": settings.evaluation_blur_threshold,
            "glare_ratio_max": settings.evaluation_glare_ratio_max,
            "min_image_side_px": settings.evaluation_min_image_side_px,
        }
    )
