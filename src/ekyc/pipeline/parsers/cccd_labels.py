# Nhãn là giả thuyết ban đầu, phải đối chiếu với thẻ thật rồi bổ sung
from dataclasses import dataclass


@dataclass(frozen=True)
class LabelSpec:
    phrases: tuple[str, ...]
    sublabels: tuple[str, ...]


LABELS = {
    "full_name": LabelSpec(("ho va ten", "ho chu dem va ten"), ("full name",)),
    "dob": LabelSpec(("ngay sinh",), ("date of birth",)),
    "gender": LabelSpec(("gioi tinh",), ("sex",)),
    "nationality": LabelSpec(("quoc tich",), ("nationality",)),
    "origin": LabelSpec(("que quan",), ("place of origin",)),
    "address": LabelSpec(("noi thuong tru", "noi cu tru"), ("place of residence",)),
    "expiry": LabelSpec(("co gia tri den",), ("date of expiry", "expiry")),
}
MULTILINE_FIELDS = frozenset({"origin", "address"})
ENGLISH_SCRUB_LABELS = tuple(
    sorted(
        {label for spec in LABELS.values() for label in spec.sublabels if len(label) >= 8},
        key=lambda label: (-len(label), label),
    )
)

RIGHT_FIELDS = frozenset({"full_name", "dob", "gender", "nationality", "origin", "address"})
LEFT_FIELDS = frozenset({"expiry"})
