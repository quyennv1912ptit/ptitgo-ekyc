"""Experimental, assisted-only GPLX parser. Historical evaluators use the v1 parser."""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from statistics import median

from rapidfuzz.distance import Levenshtein

from ekyc.config import Settings
from ekyc.pipeline.normalize import fold, fold_aligned
from ekyc.pipeline.ocr.base import OcrLine
from ekyc.pipeline.parsers.cccd_normalizers import DATE_RE, clean_name, parse_date
from ekyc.pipeline.parsers.cccd_parser import Field
from ekyc.pipeline.parsers.gplx_parser import (
    GplxFront,
    _find_spans,
    _strip_english,
    parse_gplx_front,
)

ISSUE = re.compile(r"\b(?:ngay cap|date of issue|issue date|issued on)\b", re.I)
DATE_LABEL = re.compile(
    r"\b(?:ngay sinh|date of birth|ngay cap|date of issue|issue date|issued on|"
    r"ngay het han|co gia tri den|expiration date|expiry date)\b",
    re.I,
)
CLASS_SEPARATOR = re.compile(r"[,;/]")
LABEL_TRIM = " :/.-|\t"


@dataclass(frozen=True)
class AssistedGplxFront(GplxFront):
    conflicts: tuple[str, ...] = ()


def label_noise(text: str) -> bool:
    tokens = fold(text.strip(LABEL_TRIM)).split()
    return len(tokens) == 2 and tokens[0] == "full" and Levenshtein.distance(tokens[1], "name") <= 1


def name_fix(lines, values, settings):
    if not values.raw or not label_noise(values.raw):
        return values, False
    ordered = sorted(lines, key=lambda line: (line.center_y, line.x1))
    height = max(1, median(line.y2 - line.y1 for line in ordered))
    options = {}
    for anchor in ordered:
        if not any(
            span.key == "full_name" and label_noise(anchor.text[span.end :])
            for span in _find_spans(anchor.text, settings)
        ):
            continue
        for item in ordered:
            if item is anchor or _find_spans(item.text, settings):
                continue
            dy = item.center_y - anchor.center_y
            right = abs(dy) <= 0.6 * height and item.x1 >= anchor.x2
            below = 0.6 * height < dy <= 1.8 * height and abs(item.x1 - anchor.x1) <= height
            if not (right or below) or DATE_LABEL.search(fold_aligned(item.text)):
                continue
            if below and any(
                anchor.center_y < separator.center_y <= item.center_y
                and (
                    _find_spans(separator.text, settings)
                    or ISSUE.search(fold_aligned(separator.text))
                )
                for separator in ordered
                if separator is not item
            ):
                continue
            name = clean_name(item.text)
            if name and len(name.split()) >= 2 and not label_noise(item.text):
                options[name] = item
    if len(options) == 1:
        name, item = next(iter(options.items()))
        return Field(item.text, name, item.conf, "parsed"), False
    return Field(values.raw, None, values.conf, "invalid"), len(options) > 1


def date_fix(key, field, lines, settings, today):
    if not field.raw:
        return field, False
    raw = field.raw.strip(LABEL_TRIM)
    mentions_issue = bool(ISSUE.search(fold_aligned(raw)))
    # Birth or expiry with another label in the same value must not borrow its date.
    has_foreign_date_label = bool(DATE_LABEL.search(fold_aligned(raw)))
    dates = list(DATE_RE.finditer(raw))
    if not has_foreign_date_label and len(dates) <= 1:
        return field, False
    options = {}
    for line in lines:
        spans = [span for span in _find_spans(line.text, settings) if span.key == key]
        for span in spans:
            value = _strip_english(line.text[span.end :], key, settings).strip(LABEL_TRIM)
            if boundary := DATE_LABEL.search(fold_aligned(value)):
                value = value[: boundary.start()].strip(LABEL_TRIM)
            if DATE_RE.fullmatch(value) and (day := parse_date(value)):
                if key == "expiry" or date(1900, 1, 1) <= day <= today:
                    options[day] = (value, line.conf)
    if len(options) == 1 and (mentions_issue or has_foreign_date_label):
        day, (raw, confidence) = next(iter(options.items()))
        return Field(raw, day, confidence, "parsed"), False
    return Field(field.raw, None, field.conf, "invalid"), len(dates) > 1 or len(options) > 1


def class_list(field, settings):
    if not field.raw or not CLASS_SEPARATOR.search(field.raw):
        return field
    tokens = [part.strip().upper() for part in CLASS_SEPARATOR.split(field.raw)]
    allowed = {code.upper().replace(" ", "") for code in settings.license_class_codes}
    if len(tokens) > 20 or any(token not in allowed for token in tokens):
        return Field(field.raw, None, field.conf, "invalid")
    values = tuple(dict.fromkeys(tokens))
    return Field(field.raw, values[0] if len(values) == 1 else values, field.conf, "parsed")


def parse_assisted_gplx(
    lines: Sequence[OcrLine],
    settings: Settings,
    today: date,
) -> AssistedGplxFront:
    baseline = parse_gplx_front(lines, settings, today)
    normalized = [replace(line, text=unicodedata.normalize("NFC", line.text)) for line in lines]
    v1 = parse_gplx_front(normalized, settings, today) if normalized != list(lines) else baseline
    name, name_conflict = name_fix(normalized, v1.full_name, settings)
    dob, dob_conflict = date_fix("dob", v1.dob, normalized, settings, today)
    expiry, expiry_conflict = date_fix("expiry", v1.expiry, normalized, settings, today)
    no_expiry = v1.no_expiry and expiry == v1.expiry
    if no_expiry and expiry.raw and DATE_RE.search(expiry.raw):
        expiry = Field(expiry.raw, None, expiry.conf, "invalid")
        no_expiry = False
        expiry_conflict = True
    return AssistedGplxFront(
        baseline.id_number,
        name,
        dob,
        class_list(v1.vehicle_class, settings),
        expiry,
        no_expiry,
        v1.found_labels,
        tuple(
            key
            for key, conflicted in (
                ("full_name", name_conflict),
                ("dob", dob_conflict),
                ("expiry", expiry_conflict),
            )
            if conflicted
        ),
    )
