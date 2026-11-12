from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from ekyc.config import Settings

if TYPE_CHECKING:
    from ekyc.pipeline.parsers.cccd_parser import Field

from datetime import date


def check_id_structure(id_number: str, dob: date, gender: str | None) -> list[str]:
    if len(id_number) != 12 or not id_number.isascii() or not id_number.isdigit():
        return []
    code, yy = int(id_number[3]), int(id_number[4:6])
    if code >= 4:
        return []
    result = []
    if 1900 + 100 * (code // 2) + yy != dob.year:
        result.append("birth_year")
    if gender is not None and gender != ("MALE" if code % 2 == 0 else "FEMALE"):
        result.append("gender")
    return result


REVIEW_ORDER = (
    "idNumber",
    "fullName",
    "dob",
    "gender",
    "placeOfOrigin",
    "address",
    "expiryDate",
    "issueDate",
)


def collect_review_fields(
    fields: Mapping[str, "Field"],
    settings: Settings,
    *,
    no_expiry: bool,
    mismatches: Sequence[str] = (),
) -> list[str]:
    """Gợi ý trường cần nhìn lại, không quyết định duyệt hồ sơ."""
    review = set()
    for name, field in fields.items():
        if name == "expiryDate" and no_expiry:
            continue
        threshold = (
            settings.ocr_review_conf_text
            if name in {"fullName", "placeOfOrigin", "address"}
            else settings.ocr_review_conf
        )
        if field.status != "parsed" or field.conf is None or field.conf < threshold:
            review.add(name)
    if "birth_year" in mismatches:
        review.update(("idNumber", "dob"))
    if "gender" in mismatches:
        review.update(("idNumber", "gender"))
    return [name for name in REVIEW_ORDER if name in review]
