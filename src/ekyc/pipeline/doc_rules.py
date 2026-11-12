# Từ khóa là giả thuyết ban đầu, phải đối chiếu với thẻ thật rồi bổ sung
import re
from dataclasses import dataclass

from ekyc.config import Settings
from ekyc.pipeline.doc_types import DocType, Side

# Chặn cả chuỗi số dài hơn có khoảng trắng OCR xen giữa.
ID12_RE = re.compile(r"(?<!\d)(?<!\d )\d(?: ?\d){11}(?!\d)(?! \d)")
MRZ_RE = re.compile(r"idvnm\d")


@dataclass(frozen=True)
class Feature:
    id: str
    weight: float
    phrases: tuple[str, ...] = ()
    pattern: re.Pattern[str] | None = None
    top_only: bool = False


@dataclass(frozen=True)
class Rule:
    doc_type: DocType
    side: Side
    features: tuple[Feature, ...]

    @property
    def key(self) -> str:
        return f"{self.doc_type.value.lower()}_{self.side.value.lower()}"


def build_class_pattern(codes: list[str]) -> re.Pattern[str]:
    alternatives = "|".join(
        re.escape(code)
        for code in sorted(
            {code.strip().lower() for code in codes if code.strip()},
            key=lambda code: (-len(code), code),
        )
    )
    if not alternatives:
        return re.compile(r"(?!)")
    separator = r"[\s:/.\-]+"
    return re.compile(
        r"\b(?:hang|class)(?:"
        + separator
        + r"(?:hang|class))?"
        + separator
        + r"(?:"
        + alternatives
        + r")\b"
    )


def build_rules(settings: Settings) -> tuple[Rule, ...]:
    return (
        Rule(
            DocType.CCCD,
            Side.FRONT,
            (
                Feature(
                    "title",
                    1.0,
                    ("can cuoc cong dan", "citizen identity card", "can cuoc"),
                    top_only=True,
                ),
                Feature("origin", 0.4, ("que quan", "place of origin")),
                Feature("id12", 0.3, pattern=ID12_RE),
            ),
        ),
        Rule(
            DocType.CCCD,
            Side.BACK,
            (
                Feature("identification", 1.0, ("dac diem nhan dang", "personal identification")),
                Feature("mrz", 0.8, pattern=MRZ_RE),
                Feature("issuer", 0.4, ("cuc truong cuc canh sat",)),
            ),
        ),
        Rule(
            DocType.GPLX,
            Side.FRONT,
            (
                Feature("title", 1.0, ("giay phep lai xe", "drivers license"), top_only=True),
                Feature("class", 0.4, pattern=build_class_pattern(settings.license_class_codes)),
                Feature("number", 0.2, pattern=ID12_RE),
            ),
        ),
        Rule(
            DocType.CMND,
            Side.FRONT,
            (Feature("title", 1.0, ("chung minh nhan dan",), top_only=True),),
        ),
    )
