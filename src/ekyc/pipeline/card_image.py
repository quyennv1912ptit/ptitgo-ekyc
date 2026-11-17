from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.core.inference_metrics import stage
from ekyc.pipeline.card_detector import CardDetectionResult
from ekyc.pipeline.geometry import (
    IMAGE_TOO_SMALL_MESSAGE,
    GateStatus,
    evaluate_card_gate,
    experimental_geometry_warnings,
)
from ekyc.pipeline.image_loader import LoadedImage
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
        from ekyc.pipeline.image_loader import load_image_from_url_sync

        return self.prepare_loaded(load_image_from_url_sync(url, self.settings))

    def prepare_loaded(
        self, loaded: LoadedImage, *, diagnostics: bool = False, native: bool = False
    ) -> PreparedCard:
        with stage("card_prepare_ms"):
            return self._prepare_loaded(loaded, diagnostics=diagnostics, native=native)

    def _prepare_loaded(
        self, loaded: LoadedImage, *, diagnostics: bool, native: bool
    ) -> PreparedCard:
        from ekyc.pipeline.card_detector import detect_and_warp_card

        if self.detector is not None and self.settings.card_experimental_keypoint_recovery:
            from ekyc.pipeline.keypoint_recovery import prepare_pose
            from ekyc.pipeline.yolo_card_detector import DetectionStatus

            outcome = self.detector.detect_cards(loaded.bgr)
            if outcome.status is not DetectionStatus.DETECTED or outcome.detection is None:
                details = [{"reason": outcome.failure_reason}] if outcome.failure_reason else None
                raise EkycError(outcome.error_code or ErrorCode.CARD_NOT_FOUND, details=details)
            prepared = prepare_pose(loaded.bgr, outcome.detection, self.settings)
            return self._finish_detection(prepared.crop, prepared.warnings, diagnostics, native)
        try:
            result = (
                self._detect_with_diagnostics(loaded.bgr)
                if self.detector is not None and diagnostics
                else self.detector.detect(loaded.bgr)
                if self.detector is not None
                else detect_and_warp_card(loaded)
            )
        except EkycError as exc:
            if diagnostics and self.detector is None and exc.code == ErrorCode.CARD_NOT_FOUND:
                raise preparation_error(exc, "DETECTOR_CARD_NOT_FOUND") from exc
            raise
        warnings: tuple[Warning, ...] = ()
        if self.settings.quality_profile != "calibration":
            try:
                warnings = self.gate(result.corners, loaded.width, loaded.height)
            except EkycError as exc:
                if diagnostics:
                    raise preparation_error(exc, "CARD_GEOMETRY_REJECTED") from exc
                raise
        return self._finish_detection(result, warnings, diagnostics, native)

    def _detect_with_diagnostics(self, image: np.ndarray) -> CardDetectionResult:
        from ekyc.pipeline.yolo_card_detector import rectify_corners

        outcome = self.detector.detect_cards(image)
        if outcome.detection is None:
            error = EkycError(outcome.error_code or ErrorCode.CARD_NOT_FOUND)
            if outcome.failure_reason:
                raise preparation_error(error, outcome.failure_reason)
            raise error
        card = outcome.detection
        confidence = card.keypoint_confidence
        if confidence is None:
            confidence = np.ones(4, np.float32)
        try:
            return rectify_corners(image, card.quad_native, confidence, self.settings)
        except EkycError as exc:
            if exc.code == ErrorCode.INVALID_IMAGE:
                reason = "CARD_WARP_FAILED"
            elif np.any(
                ~np.isfinite(confidence) | (confidence < self.settings.card_yolo_keypoint_conf)
            ):
                reason = "KEYPOINT_CONFIDENCE_REJECTED"
            else:
                reason = "CARD_RECTIFICATION_REJECTED"
            raise preparation_error(exc, reason) from exc
        except cv2.error as exc:
            raise preparation_error(EkycError(ErrorCode.INVALID_IMAGE), "CARD_WARP_FAILED") from exc

    def _finish_detection(
        self,
        result: CardDetectionResult,
        warnings: tuple[Warning, ...],
        diagnostics: bool,
        native: bool,
    ) -> PreparedCard:
        try:
            image = (
                self.landscape(result.warped_image) if native else self.finish(result.warped_image)
            )
        except EkycError as exc:
            if diagnostics:
                raise preparation_error(exc, "CARD_QUALITY_REJECTED") from exc
            raise
        if self.detector is not None and self.settings.quality_profile != "calibration":
            warnings += experimental_geometry_warnings(result.corners, self.settings)
        return PreparedCard(image, warnings)

    def gate(self, quad: np.ndarray, width: int, height: int) -> tuple[Warning, ...]:
        """Gate hình học trên ảnh gốc; BLOCK ném lỗi, PASS_REVIEW trả cảnh báo."""
        outcome = evaluate_card_gate(quad, width, height, self.settings)
        if outcome.status is GateStatus.BLOCK:
            if outcome.error_code is ErrorCode.IMAGE_TOO_SMALL:
                raise EkycError(ErrorCode.IMAGE_TOO_SMALL, IMAGE_TOO_SMALL_MESSAGE)
            raise EkycError(outcome.error_code or ErrorCode.CARD_NOT_FOUND)
        return outcome.warnings

    @staticmethod
    def landscape(image: np.ndarray) -> np.ndarray:
        # Xoay hình dọc sang ngang; hướng chữ 180 độ vẫn do luồng OCR hiện tại xử lý.
        if image.shape[0] > image.shape[1]:
            return cv2.rotate(image, cv2.ROTATE_90_CLOCKWISE)
        return image

    def finish(self, image: np.ndarray) -> np.ndarray:
        image = self.landscape(image)
        from ekyc.pipeline.quality import check_warped_quality

        check_warped_quality(image, quality_settings(self.settings))
        if self.settings.quality_profile == "calibration":
            return image
        return cv2.resize(
            image,
            (self.settings.card_output_width, self.settings.card_output_height),
            interpolation=cv2.INTER_AREA,
        )


def preparation_error(error: EkycError, reason: str) -> EkycError:
    return EkycError(error.code, error.message, details=[{"reason": reason}])


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
