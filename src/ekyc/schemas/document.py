from typing import Literal

from pydantic import Field, JsonValue

from ekyc.schemas.common import CamelModel

DocumentSlot = Literal["cccd_front", "cccd_back", "gplx_front", "gplx_back"]


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
    candidates: list[Candidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    field_statuses: dict[str, str] = Field(default_factory=dict)
    conflict_fields: list[str] = Field(default_factory=list)
