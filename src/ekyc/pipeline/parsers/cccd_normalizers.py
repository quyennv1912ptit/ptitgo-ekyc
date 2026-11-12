import re
import unicodedata
from collections.abc import Sequence
from datetime import date

from ekyc.pipeline.doc_rules import ID12_RE
from ekyc.pipeline.normalize import fold
from ekyc.pipeline.ocr.base import OcrLine

DIGIT_TRANSLATION = str.maketrans(
    {"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8", "Z": "2"}
)
DIGITS = r"0-9OolI|SBZ"
DATE_RE = re.compile(
    r"(?<!["
    + DIGITS
    + r"])(["
    + DIGITS
    + r"]{1,2})\s*[/.-]\s*(["
    + DIGITS
    + r"]{1,2})\s*[/.-]\s*(["
    + DIGITS
    + r"]{4})(?!["
    + DIGITS
    + r"])"
)
ID_CANDIDATE_RE = re.compile(
    r"(?<!["
    + DIGITS
    + r"])(?<!["
    + DIGITS
    + r"] )["
    + DIGITS
    + r"](?: ?["
    + DIGITS
    + r"]){11}(?!["
    + DIGITS
    + r"])(?! ["
    + DIGITS
    + r"])"
)
GENDER_RE = re.compile(r"\b(nam|nu)\b")


def fix_digit_confusions(token: str) -> str:
    """Sửa nhầm ký tự trên đoạn đã nhận dạng là số/ngày."""
    return token.translate(DIGIT_TRANSLATION)


def parse_date(text: str) -> date | None:
    """Lấy ngày hợp lệ đầu tiên, chỉ sửa ký tự trong nhóm số."""
    for match in DATE_RE.finditer(text):
        day, month, year = (int(fix_digit_confusions(item)) for item in match.groups())
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def extract_id_number(lines: Sequence[OcrLine]) -> tuple[str, float] | None:
    """Lấy số 12 chữ số có confidence cao nhất; hòa chọn dòng trên."""
    candidates: list[tuple[str, float]] = []
    for line in sorted(lines, key=lambda item: (item.center_y, item.x1)):
        matches = list(ID12_RE.finditer(line.text))
        if matches:
            candidates.extend((re.sub(r"\s", "", match.group()), line.conf) for match in matches)
            continue
        for match in ID_CANDIDATE_RE.finditer(line.text):
            token = match.group()
            if sum(char in "0123456789" for char in token) >= 8:
                fixed = fix_digit_confusions(token).replace(" ", "")
                if ID12_RE.fullmatch(fixed):
                    candidates.append((fixed, line.conf))
    return max(candidates, key=lambda item: item[1], default=None)


def clean_name(text: str) -> str:
    """Giữ chữ có dấu và dấu nối/nháy; tên có số là không hợp lệ."""
    text = unicodedata.normalize("NFC", text)
    if any(char.isdigit() for char in text):
        return ""
    return " ".join(
        "".join(char for char in text if char.isalpha() or char.isspace() or char in "'-").split()
    ).upper()


def parse_gender(text: str) -> str | None:
    """Bỏ quốc tịch Việt Nam trước khi tìm token giới tính."""
    match = GENDER_RE.search(fold(text).replace("viet nam", ""))
    return {"nam": "MALE", "nu": "FEMALE"}[match.group()] if match else None


def clean_address(lines: Sequence[str]) -> str:
    """Làm sạch từng dòng và nối địa chỉ bằng dấu phẩy."""
    return ", ".join(value for line in lines if (value := " ".join(line.strip(" ,\t\r\n").split())))


def is_no_expiry(text: str) -> bool:
    """Nhận diện cụm không thời hạn theo chữ đã fold."""
    return "khong thoi han" in fold(text)
