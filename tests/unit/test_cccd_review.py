from datetime import date

from ekyc.config import Settings
from ekyc.pipeline.parsers.cccd_checks import REVIEW_ORDER, collect_review_fields
from ekyc.pipeline.parsers.cccd_parser import Field


def test_review_order_and_thresholds(settings: Settings) -> None:
    fields = {name: Field("fake", "fake", 0.99, "parsed") for name in reversed(REVIEW_ORDER)}
    assert collect_review_fields(fields, settings, no_expiry=False) == []
    fields["address"] = Field("fake", "fake", 0.89, "parsed")
    fields["issueDate"] = Field("fake", date(2020, 1, 1), 0.46, "parsed")
    assert collect_review_fields(fields, settings, no_expiry=False) == ["address", "issueDate"]
    assert collect_review_fields(
        fields, settings, no_expiry=False, mismatches=["birth_year", "gender"]
    ) == ["idNumber", "dob", "gender", "address", "issueDate"]


def test_missing_none_no_expiry(settings: Settings) -> None:
    fields = {
        "expiryDate": Field(None, None, None, "missing"),
        "issueDate": Field(None, None, None, "missing"),
        "fullName": Field("fake", "fake", None, "parsed"),
        "address": Field("fake", None, 0.99, "invalid"),
    }
    assert collect_review_fields(fields, settings, no_expiry=True) == [
        "fullName",
        "address",
        "issueDate",
    ]
