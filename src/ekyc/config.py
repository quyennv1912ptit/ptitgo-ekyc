from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    app_name: str = "ekyc-service"
    env: str = "dev"
    log_level: str = "INFO"
    ekyc_api_key: SecretStr
    allowed_image_hosts: Annotated[list[str], NoDecode] = ["res.cloudinary.com"]
    max_image_bytes: int = Field(default=8_000_000, gt=0)
    max_image_pixels: int = Field(default=40_000_000, gt=0)
    fetch_timeout_s: float = Field(default=10, gt=0)
    max_concurrent_jobs: int = Field(default=2, gt=0)
    job_slot_timeout_s: float = Field(default=0.5, ge=0)
    min_image_side_px: int = Field(default=1000, gt=0)
    min_brightness: float = Field(default=40, ge=0, le=255)
    blur_threshold: float = Field(default=100, ge=0)
    glare_ratio_max: float = Field(default=0.04, ge=0, le=1)
    card_detector_backend: Literal["opencv", "yolo"] = "opencv"
    card_yolo_model_path: str = "models/card_pose_v3_best.pt"
    card_yolo_imgsz: int = Field(default=640, ge=64)
    card_yolo_conf: float = Field(default=0.25, gt=0, le=1)
    card_yolo_keypoint_conf: float = Field(default=0.4, gt=0, le=1)
    card_yolo_min_area_ratio: float = Field(default=0.02, gt=0, le=1)
    card_yolo_border_ratio: float = Field(default=0.003, ge=0, lt=0.1)
    card_aspect_ratio_min: float = Field(default=1.2, gt=1)
    card_aspect_ratio_max: float = Field(default=2.1, gt=1)
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
    name_match_block: float = Field(default=0.85, ge=0, le=1)
    name_match_warn: float = Field(default=0.95, ge=0, le=1)
    face_model: str = "ArcFace"
    face_detector: str = "retinaface"

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

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if not self.ekyc_api_key.get_secret_value().strip():
            raise ValueError("EKYC_API_KEY must not be empty")
        if self.name_match_block > self.name_match_warn:
            raise ValueError("NAME_MATCH_BLOCK must be <= NAME_MATCH_WARN")
        if self.card_aspect_ratio_min > self.card_aspect_ratio_max:
            raise ValueError("CARD_ASPECT_RATIO_MIN must be <= CARD_ASPECT_RATIO_MAX")
        if self.card_min_long_side_px > self.card_ok_long_side_px:
            raise ValueError("CARD_MIN_LONG_SIDE_PX must be <= CARD_OK_LONG_SIDE_PX")
        if self.card_output_width <= self.card_output_height:
            raise ValueError("Card output must be landscape")
        if self.env == "prod" and self.quality_profile == "evaluation":
            raise ValueError("Evaluation quality profile must not run in prod")
        if self.env == "prod" and self.quality_profile == "calibration":
            raise ValueError("Calibration quality profile must not run in prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
