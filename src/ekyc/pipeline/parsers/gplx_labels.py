# Nhãn lấy từ dump OCR 13 ảnh GPLX front (private), không suy đoán từ trí nhớ.
from dataclasses import dataclass


@dataclass(frozen=True)
class LabelSpec:
    phrases: tuple[str, ...]
    english: tuple[str, ...]


LABELS = {
    "id_number": LabelSpec(("so/no", "so / no", "so/gplx", "so gplx"), ("no.",)),
    "full_name": LabelSpec(("ho ten", "ho va ten"), ("full name",)),
    "dob": LabelSpec(("ngay sinh",), ("date of birth",)),
    "vehicle_class": LabelSpec(("hang class", "hangclass", "hang"), ("class",)),
    "expiry": LabelSpec(("ngay het han", "co gia tri den"), ("expiration date", "expiry")),
}

# Nhãn tiếng Anh đủ dài mới được gỡ khỏi giá trị (tránh cắt nhầm tên/địa danh).
ENGLISH_SCRUB_LABELS = tuple(
    sorted(
        {label for spec in LABELS.values() for label in spec.english if len(label) >= 8},
        key=lambda label: (-len(label), label),
    )
)
