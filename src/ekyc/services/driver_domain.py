import unicodedata
from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

SLOTS = {"cccd_front", "cccd_back", "gplx_front", "gplx_back", "selfie"}
REQUIRED = SLOTS - {"gplx_back"}


class Details(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    cccd_name: str = Field(min_length=1, max_length=150)
    cccd_id: str = Field(pattern=r"^[0-9]{12}$")
    cccd_dob: date
    cccd_expiry_date: date | None = None
    cccd_gender: str = Field(default="", pattern=r"^(MALE|FEMALE)?$")
    gplx_number: str = Field(min_length=1, max_length=30)
    gplx_name: str = Field(min_length=1, max_length=150)
    gplx_dob: date
    gplx_classes: list[Annotated[str, Field(pattern=r"^[A-Z0-9]{1,8}$")]] = Field(
        min_length=1, max_length=20
    )
    no_expiry: bool = Field(strict=True)
    expiry_date: date | None

    def valid(self):
        return self.no_expiry == (self.expiry_date is None)


def canonical_details(values):
    result = dict(values)
    if "gplx_expiry" in result and not {"no_expiry", "expiry_date"} & result.keys():
        expiry = result.pop("gplx_expiry")
        result.update(
            no_expiry=expiry == "NO_EXPIRY", expiry_date=None if expiry == "NO_EXPIRY" else expiry
        )
    return result


def normalized_name(value):
    return " ".join(unicodedata.normalize("NFC", value).split()).lower()


def name_comparison(left, right):
    if not left or not right:
        return None
    left, right = normalized_name(left), normalized_name(right)
    if left == right:
        return "EXACT_MATCH"

    def fold_marks(value):
        return "".join(
            ch for ch in unicodedata.normalize("NFD", value) if not unicodedata.combining(ch)
        ).replace("đ", "d")

    return "DIACRITIC_MISMATCH" if fold_marks(left) == fold_marks(right) else "NAME_MISMATCH"


def comparison_warnings(values):
    name = name_comparison(values.get("cccd_name"), values.get("gplx_name"))
    left, right = values.get("cccd_dob"), values.get("gplx_dob")
    dob = bool(left and right and left.strip() != right.strip())
    if name == "NAME_MISMATCH" and dob:
        return ["NAME_AND_DOB_MISMATCH"]
    warnings = [name] if name in {"DIACRITIC_MISMATCH", "NAME_MISMATCH"} else []
    return warnings + (["DOB_MISMATCH"] if dob else [])


def conflict_code(values):
    return next(iter(comparison_warnings(values)), None)
