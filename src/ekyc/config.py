from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="", extra="ignore", hide_input_in_errors=True
    )

    app_name: str = "ekyc-service"
    app_version: str | None = None
    git_commit_sha: str | None = None
    env: Literal["demo", "dev", "staging", "prod"] = "dev"
    log_level: str = "INFO"
    firebase_project_id: str | None = None
    verification_sync_key: SecretStr | None = None
    face_comparison_enabled: bool = False
    face_comparison_manifest: Path | None = None
    verification_admin_origins: list[str] = Field(default_factory=list)
    verification_db_path: Path | None = None
    verification_storage_dir: Path | None = None
    verification_storage_key: SecretStr | None = None
    verification_uploads_per_minute: int = Field(default=12, ge=1)
    verification_parallel_uploads: int = Field(default=2, ge=1, le=16)
    verification_upload_timeout_s: float = Field(default=30, gt=0)
    verification_retired_days: int = Field(default=7, ge=1)
    verification_terminal_days: int | None = Field(default=None, ge=1)
    verification_orphan_hours: int = Field(default=24, ge=1)
    ekyc_api_key: SecretStr
    allowed_image_hosts: Annotated[list[str], NoDecode] = ["res.cloudinary.com"]
    max_image_bytes: int = Field(default=8_000_000, gt=0)
    max_image_pixels: int = Field(default=40_000_000, gt=0)
    fetch_timeout_s: float = Field(default=10, gt=0)
    inference_ocr_factory: str | None = None
    inference_warm_up: bool = False
    max_concurrent_jobs: int = Field(default=2, gt=0)
    job_slot_timeout_s: float = Field(default=0.5, ge=0)
    min_image_side_px: int = Field(default=1000, gt=0)
    min_brightness: float = Field(default=40, ge=0, le=255)
    blur_threshold: float = Field(default=100, ge=0)
    glare_ratio_max: float = Field(default=0.04, ge=0, le=1)
    # Mặc định tự chọn yolo khi có checkpoint; opencv là backend legacy.
    card_detector_backend: Literal["opencv", "yolo"] | None = None
    card_yolo_model_path: str = "models/card_pose_v3_best.pt"
    card_yolo_imgsz: int = Field(default=640, ge=64)
    card_yolo_conf: float = Field(default=0.25, gt=0, le=1)
    card_yolo_keypoint_conf: float = Field(default=0.4, gt=0, le=1)
    card_experimental_keypoint_recovery: bool = False
    card_experimental_geometry_review: bool = False
    card_yolo_min_area_ratio: float = Field(default=0.02, gt=0, le=1)
    card_aspect_ratio_min: float = Field(default=1.2, gt=1)
    card_aspect_ratio_max: float = Field(default=2.1, gt=1)
    # Ngưỡng khởi điểm cho phát hiện nhiều thẻ và fallback xoay; cần chỉnh bằng ảnh thật.
    card_conf_primary: float = Field(default=0.25, gt=0, le=1)
    card_conf_rotated: float = Field(default=0.35, gt=0, le=1)
    card_aspect_min: float = Field(default=1.15, gt=1)
    card_aspect_max: float = Field(default=2.3, gt=1)
    card_dup_iou: float = Field(default=0.5, ge=0, le=1)
    card_contain_ratio: float = Field(default=0.8, ge=0, le=1)
    card_secondary_max_overlap: float = Field(default=0.1, ge=0, le=1)
    card_secondary_min_conf: float = Field(default=0.5, gt=0, le=1)
    card_secondary_min_area_ratio: float = Field(default=0.15, gt=0, le=1)
    card_output_width: int = Field(default=1600, ge=64)
    card_output_height: int = Field(default=1009, ge=64)
    # Ngưỡng khởi điểm cho gate hình học trước warp; cần chỉnh bằng ảnh thật.
    card_min_long_side_px: int = Field(default=600, gt=0)
    card_ok_long_side_px: int = Field(default=1000, gt=0)
    card_edge_margin_px: int = Field(default=5, ge=0)
    quality_profile: Literal["strict", "evaluation", "calibration"] = "strict"
    evaluation_blur_threshold: float = Field(default=20, ge=0)
    evaluation_glare_ratio_max: float = Field(default=0.15, ge=0, le=1)
    evaluation_min_image_side_px: int = Field(default=320, gt=0)

    easyocr_model_dir: Path | None = None
    ocr_backend: Literal["easyocr", "none"] = "easyocr"
    ocr_languages: Annotated[list[str], NoDecode] = ["vi", "en"]
    ocr_max_side_px: int = Field(default=1600, gt=0)
    ocr_retry_rotate_180: bool = True
    line_row_tolerance: float = Field(default=0.5, ge=0)
    line_gap_ratio: float = Field(default=1.5, ge=0)
    label_fuzzy_cutoff: int = Field(default=80, ge=0, le=100)
    field_gap_ratio: float = Field(default=2.5, ge=0)
    cccd_multiline_max: int = Field(default=3, ge=1)
    ocr_field_warn_conf: float = Field(default=0.6, ge=0, le=1)
    # Giá trị khởi điểm, cần chỉnh bằng ảnh thật.
    scrub_fuzzy_cutoff: int = Field(default=72, ge=0, le=100)
    cccd_col_split_ratio: float = Field(default=2.5, ge=0)
    cccd_min_anchor_lines: int = Field(default=2, ge=1)
    # None dùng mặc định EasyOCR; thử 0.2 nếu box thô gộp hai cột.
    ocr_width_ths: float | None = Field(default=None, ge=0)
    ocr_review_conf: float = Field(default=0.85, ge=0, le=1)
    ocr_review_conf_text: float = Field(default=0.9, ge=0, le=1)
    cccd_timeout_s: float = Field(default=25, gt=0)
    timezone: str = "Asia/Ho_Chi_Minh"

    @field_validator("ocr_width_ths", mode="before")
    @classmethod
    def parse_ocr_width_ths(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("ocr_languages", mode="before")
    @classmethod
    def parse_ocr_languages(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    doc_fuzzy_cutoff: int = Field(default=85, ge=0, le=100)
    doc_accept_score: float = Field(default=0.7, ge=0, le=1)
    doc_margin: float = Field(default=0.3, ge=0, le=1)
    doc_side_margin: float = Field(default=0.2, ge=0, le=1)
    doc_min_text_chars: int = Field(default=30, ge=0)
    doc_min_line_conf: float = Field(default=0.3, ge=0, le=1)
    doc_top_fraction: float = Field(default=0.4, ge=0, le=1)
    # Hạng bằng đã thay đổi theo quy định mới; đây chỉ là danh sách nhận diện chữ ban đầu,
    # không phải nguồn pháp lý, cần chỉnh theo thẻ thật.
    license_class_codes: Annotated[list[str], NoDecode] = [
        "A1",
        "A2",
        "A3",
        "A4",
        "A",
        "B1",
        "B2",
        "B",
        "BE",
        "C1",
        "C",
        "C1E",
        "CE",
        "D1",
        "D2",
        "D",
        "D1E",
        "D2E",
        "DE",
        "E",
        "FB2",
        "FC",
        "FD",
        "FE",
    ]

    @field_validator("license_class_codes", mode="before")
    @classmethod
    def parse_license_classes(cls, value: object) -> object:
        if isinstance(value, str):
            return [code.strip() for code in value.split(",") if code.strip()]
        return value

    @field_validator("allowed_image_hosts", mode="before")
    @classmethod
    def parse_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [host.strip().lower() for host in value.split(",") if host.strip()]
        return value

    @property
    def deployed(self) -> bool:
        return self.env in {"staging", "prod"}

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if not self.ekyc_api_key.get_secret_value().strip():
            raise ValueError("EKYC_API_KEY must not be empty")
        if self.card_aspect_ratio_min > self.card_aspect_ratio_max:
            raise ValueError("CARD_ASPECT_RATIO_MIN must be <= CARD_ASPECT_RATIO_MAX")
        if self.card_min_long_side_px > self.card_ok_long_side_px:
            raise ValueError("CARD_MIN_LONG_SIDE_PX must be <= CARD_OK_LONG_SIDE_PX")
        if self.card_aspect_min > self.card_aspect_max:
            raise ValueError("CARD_ASPECT_MIN must be <= CARD_ASPECT_MAX")
        if self.card_output_width <= self.card_output_height:
            raise ValueError("Card output must be landscape")
        if self.deployed and self.quality_profile != "strict":
            raise ValueError("Staging and production require strict quality checks")
        if self.deployed and (
            self.card_experimental_keypoint_recovery
            or self.card_experimental_geometry_review
            or self.face_comparison_enabled
            or self.inference_ocr_factory
        ):
            raise ValueError("Experimental inference configuration is restricted to local use")
        if self.deployed and len(self.ekyc_api_key.get_secret_value().strip()) < 32:
            raise ValueError("Configure a staging or production API key of at least 32 characters")
        if self.card_detector_backend is None:
            available = Path(self.card_yolo_model_path).is_file()
            self.card_detector_backend = "yolo" if self.deployed or available else "opencv"
        if (
            not self.deployed
            and self.card_detector_backend == "yolo"
            and not Path(self.card_yolo_model_path).is_file()
        ):
            raise ValueError("CARD_DETECTOR_BACKEND=yolo requires a local checkpoint")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
