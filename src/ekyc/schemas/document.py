from typing import Literal

from pydantic import Field, JsonValue

from ekyc.schemas.common import CamelModel

DocumentSlot = Literal["cccd_front", "cccd_back", "gplx_front", "gplx_back"]
QrStatus = Literal[
    "NOT_FOUND",
    "DETECTED_UNREADABLE",
    "DECODED_VALID",
    "INVALID_FORMAT",
    "UNSUPPORTED_FORMAT",
    "CONFLICT",
    "ENGINE_UNAVAILABLE",
]
QrSource = Literal["QR_IMAGE", "QR_LIVE_SCAN"]
QrDocumentSide = Literal["FRONT", "BACK", "UNKNOWN"]
QrCardVariant = Literal["CCCD_2021", "CAN_CUOC_2024", "GPLX_PET", "UNKNOWN"]
QrCrossValidationStatus = Literal[
    "MATCHED",
    "CONFLICT",
    "INSUFFICIENT_EVIDENCE",
    "NOT_APPLICABLE",
]


class QrResult(CamelModel):
    status: QrStatus
    source: QrSource = "QR_IMAGE"
    document_side: QrDocumentSide = "UNKNOWN"
    card_variant: QrCardVariant | None = None
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    conflict_fields: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    cross_validation_status: QrCrossValidationStatus = "NOT_APPLICABLE"
    attachment_id: str | None = None
    revision: int | None = None
    bound_to_capture: bool = False


class Candidate(CamelModel):
    field_key: str
    candidate_value: JsonValue = None
    source: Literal["QR", "OCR"]
    source_document: Literal["CCCD", "GPLX"]
    source_side: Literal["FRONT", "BACK"]
    engine_confidence: float | None = None
    parse_status: Literal["parsed", "missing", "invalid", "unknown"]
    validation_status: Literal["valid", "invalid", "unknown"] = "unknown"
    warnings: list[str] = Field(default_factory=list)
    conflict: bool = False


class ExtractionResult(CamelModel):
    slot: DocumentSlot
    qr_status: str = "SKIPPED"
    ocr_status: str = "SKIPPED"
    qr: QrResult | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    field_statuses: dict[str, str] = Field(default_factory=dict)
    conflict_fields: list[str] = Field(default_factory=list)
