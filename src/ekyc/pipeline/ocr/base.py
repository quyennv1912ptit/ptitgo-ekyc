from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class OcrLine:
    text: str
    conf: float
    x1: float
    y1: float
    x2: float
    y2: float
    parts: tuple["OcrLine", ...] = ()

    def __post_init__(self) -> None:
        if not 0 <= self.conf <= 1:
            raise ValueError("conf must be in [0, 1]")

    @property
    def center_y(self) -> float:
        return (self.y1 + self.y2) / 2


class OcrEngine(Protocol):
    def read_lines(self, image: Any) -> list[OcrLine]: ...
