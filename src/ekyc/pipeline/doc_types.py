from dataclasses import dataclass
from enum import Enum


class DocType(str, Enum):  # noqa: UP042 - Giữ kiểu enum theo đặc tả.
    CCCD = "CCCD"
    GPLX = "GPLX"
    CMND = "CMND"
    UNKNOWN = "UNKNOWN"


class Side(str, Enum):  # noqa: UP042 - Giữ kiểu enum theo đặc tả.
    FRONT = "FRONT"
    BACK = "BACK"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class DocClassification:
    doc_type: DocType
    side: Side
    score: float
    runner_up: float
    evidence: list[str]
    readable: bool
