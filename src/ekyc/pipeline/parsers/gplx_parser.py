import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from statistics import median
from typing import Any, Literal

from rapidfuzz import fuzz

from ekyc.config import Settings
from ekyc.pipeline.normalize import fold, fold_aligned
from ekyc.pipeline.ocr.base import OcrLine
from ekyc.pipeline.parsers.cccd_normalizers import (
    clean_name,
    fix_digit_confusions,
    parse_date,
)
from ekyc.pipeline.parsers.cccd_parser import Field
from ekyc.pipeline.parsers.gplx_labels import LABELS

GplxStatus = Literal["parsed", "missing", "invalid"]
NO_EXPIRY_PHRASES = ("khong gioi han", "khong thoi han")


@dataclass(frozen=True)
class GplxFront:
    id_number: Field
    full_name: Field
    dob: Field
    vehicle_class: Field
    expiry: Field
    no_expiry: bool
    found_labels: tuple[str, ...]


@dataclass(frozen=True)
class LabelSpan:
    key: str
    start: int
    end: int


def _aligned(phrase: str, folded: str, cutoff: int):
    match = fuzz.partial_ratio_alignment(phrase, folded, score_cutoff=cutoff)
    if not match:
        return None
    window = folded[match.dest_start : match.dest_end]
    if fuzz.ratio(phrase, window, score_cutoff=cutoff) == 0:
        return None
    return match


def _find_spans(text: str, settings: Settings) -> list[LabelSpan]:
    """Chỉ nhãn tiếng Việt, neo ở đầu dòng; nhãn Anh được gỡ khỏi giá trị sau đó."""
    folded = fold_aligned(text)
    spans: list[LabelSpan] = []
    for key, spec in LABELS.items():
        for phrase in spec.phrases:
            if len(folded) < max(4, 0.6 * len(phrase)):
                continue
            match = _aligned(phrase, folded, settings.label_fuzzy_cutoff)
            if (
                match
                and match.dest_start <= 4
                and match.dest_end - match.dest_start >= 0.6 * len(phrase)
            ):
                spans.append(LabelSpan(key, match.dest_start, match.dest_end))
    unique = {(span.key, span.start): span for span in spans}
    return sorted(unique.values(), key=lambda span: (span.start, span.end))


def _strip_english(text: str, key: str, settings: Settings) -> str:
    """Bỏ nhãn Anh nằm ngay sau nhãn Việt trên cùng dòng (vd. 'Họ tên Full name: ...')."""
    stripped = text.lstrip(" :/.-|\t")
    folded = fold_aligned(stripped)
    best = None
    for phrase in LABELS[key].english:
        if len(folded) < len(phrase):
            continue
        match = _aligned(phrase, folded, settings.label_fuzzy_cutoff)
        if (
            match
            and match.dest_start <= 3
            and match.dest_end - match.dest_start >= 0.7 * len(phrase)
            and (best is None or match.dest_start < best.dest_start)
        ):
            best = match
    return stripped[best.dest_end :] if best else stripped


def _append(
    candidates: dict[str, list[tuple[str, float]]], key: str, text: str, conf: float
) -> None:
    value = text.strip(" :/.-|\t")
    if sum(char.isalnum() for char in value) >= 2:
        candidates.setdefault(key, []).append((value, conf))


def _collect(
    lines: Sequence[OcrLine], settings: Settings
) -> tuple[dict[str, str], dict[str, float]]:
    ordered = sorted(lines, key=lambda line: (line.center_y, line.x1))
    height = median(line.y2 - line.y1 for line in ordered) if ordered else 0.0
    height = max(height, 1.0)
    width = max((line.x2 for line in ordered), default=1.0)
    candidates: dict[str, list[tuple[str, float]]] = {}
    opened: list[tuple[str, int, float, float, float]] = []
    for index, line in enumerate(ordered):
        spans = _find_spans(line.text, settings)
        if not spans:
            continue
        for position, span in enumerate(spans):
            boundary = spans[position + 1].start if position + 1 < len(spans) else len(line.text)
            inline = line.text[span.end : boundary]
            inline = _strip_english(inline, span.key, settings)
            if sum(char.isalnum() for char in inline) >= 2:
                _append(candidates, span.key, inline, line.conf)
            else:
                opened.append((span.key, index, line.x1, line.x2, line.center_y))
    for key, index, x1, x2, center_y in opened:
        if key in candidates:
            continue
        # Giá trị thường nằm cùng hàng, lệch phải (box OCR tách khỏi nhãn).
        same_row = [
            line
            for position, line in enumerate(ordered)
            if position != index
            and abs(line.center_y - center_y) <= 0.6 * height
            and line.x1 >= x2 - 0.02 * width
            and not _find_spans(line.text, settings)
        ]
        if same_row:
            best = min(same_row, key=lambda line: (abs(line.center_y - center_y), -line.conf))
            _append(candidates, key, _strip_english(best.text, key, settings), best.conf)
            continue
        # Hoặc nằm ngay dòng dưới, cùng cột với nhãn.
        for line in ordered[index + 1 :]:
            if line.center_y - center_y > 1.8 * height:
                break
            if _find_spans(line.text, settings):
                continue
            if line.x1 >= x1 - 0.15 * height and line.x1 <= x1 + 0.6 * width:
                _append(candidates, key, _strip_english(line.text, key, settings), line.conf)
                break
    values = {key: max(items, key=lambda item: item[1])[0] for key, items in candidates.items()}
    confidences = {
        key: max(items, key=lambda item: item[1])[1] for key, items in candidates.items()
    }
    return values, confidences


def _vehicle_class(raw: str, settings: Settings) -> str | None:
    token = re.sub(r"[^A-Z0-9]", "", unicodedata.normalize("NFC", raw).upper())
    codes = {code.upper().replace(" ", "") for code in settings.license_class_codes}
    if token in codes:
        return token
    # Chỉ map nhiễu đã gặp trên ảnh chẩn đoán: "Al" -> "A1".
    for source, target in (("L", "1"), ("I", "1"), ("O", "0")):
        candidate = token.replace(source, target)
        if candidate in codes:
            return candidate
    return None


def _is_no_expiry(text: str, settings: Settings) -> bool:
    folded = fold(text)
    return any(
        fuzz.partial_ratio(phrase, folded, score_cutoff=settings.label_fuzzy_cutoff) != 0
        for phrase in NO_EXPIRY_PHRASES
    )


def _build(
    key: str,
    raw: str | None,
    confidence: float | None,
    settings: Settings,
    today: date,
) -> tuple[Field, bool]:
    if raw is None:
        return Field(None, None, None, "missing"), False
    value: Any = None
    no_expiry = False
    if key == "id_number":
        token = fix_digit_confusions(re.sub(r"[\s.\-/]", "", raw.upper()))
        value = token if re.fullmatch(r"\d{6,15}", token) else None
    elif key == "full_name":
        value = clean_name(raw) or None
    elif key == "dob":
        value = parse_date(raw)
        if value and (value.year < 1900 or value > today):
            value = None
    elif key == "vehicle_class":
        value = _vehicle_class(raw, settings)
    elif key == "expiry":
        no_expiry = _is_no_expiry(raw, settings)
        value = None if no_expiry else parse_date(raw)
    status: GplxStatus = "parsed" if value is not None or no_expiry else "invalid"
    return Field(raw, value, confidence, status), no_expiry


def parse_gplx_front(
    lines: Sequence[OcrLine], settings: Settings, today: date | None = None
) -> GplxFront:
    today = today or date.today()
    values, confidences = _collect(lines, settings)
    found = tuple(key for key in LABELS if key in values)
    fields: dict[str, Field] = {}
    no_expiry_any = False
    for key in LABELS:
        field, no_expiry = _build(key, values.get(key), confidences.get(key), settings, today)
        fields[key] = field
        no_expiry_any = no_expiry_any or no_expiry
    return GplxFront(
        id_number=fields["id_number"],
        full_name=fields["full_name"],
        dob=fields["dob"],
        vehicle_class=fields["vehicle_class"],
        expiry=fields["expiry"],
        no_expiry=no_expiry_any,
        found_labels=found,
    )
