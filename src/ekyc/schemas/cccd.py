from datetime import date
from typing import Literal

from pydantic import Field

from ekyc.schemas.common import CamelModel


class CccdRequest(CamelModel):
    front_url: str = Field(min_length=10, max_length=2048)
    back_url: str = Field(min_length=10, max_length=2048)


class CccdData(CamelModel):
    """Review: chỉ để tô sáng trường cần xem kỹ; KHÔNG dùng để tự động duyệt hồ sơ."""

    id_number: str
    full_name: str
    dob: date
    gender: Literal["MALE", "FEMALE"] | None = None
    address: str | None = None
    place_of_origin: str | None = None
    expiry_date: date | None = None
    no_expiry: bool
    issue_date: date | None = None
    field_confidence: dict[str, float]
    review_fields: list[str]
    review_required: bool
