import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from statistics import median
from typing import Any, Literal

from rapidfuzz import fuzz

from ekyc.config import Settings
from ekyc.pipeline.layout import Fragment, build_fragments
from ekyc.pipeline.normalize import fold_aligned
from ekyc.pipeline.ocr.base import OcrLine
from ekyc.pipeline.parsers.cccd_labels import LABELS, LEFT_FIELDS, MULTILINE_FIELDS, RIGHT_FIELDS
from ekyc.pipeline.parsers.cccd_normalizers import (
    clean_address,
    clean_name,
    extract_id_number,
    is_no_expiry,
    parse_date,
    parse_gender,
)
from ekyc.pipeline.parsers.label_scrub import is_label_only, scrub_labels


@dataclass(frozen=True)
class Field:
    """`parsed` chỉ nghĩa là parser lấy được giá trị đúng định dạng,
    không bảo đảm giá trị chính xác.
    """

    raw: str | None
    value: Any | None
    conf: float | None
    status: Literal["parsed", "missing", "invalid"]


@dataclass(frozen=True)
class CccdFront:
    id_number: Field
    full_name: Field
    dob: Field
    gender: Field
    origin: Field
    address: Field
    expiry: Field
    no_expiry: bool
    layout_reliable: bool


@dataclass(frozen=True)
class CccdBack:
    issue_date: Field


@dataclass(frozen=True)
class LabelSpan:
    key: str
    start: int
    end: int


@dataclass
class ParseDebug:
    scrubbed: list[tuple[str, str]] = field(default_factory=list)
    x_right: float | None = None
    x_split: float | None = None
    fragments: list[Fragment] = field(default_factory=list)
    used_fragments: dict[str, tuple[int, ...]] = field(default_factory=dict)
    label_splits: list[int] = field(default_factory=list)


def _find_spans(text: str, *, english: bool, settings: Settings) -> list[LabelSpan]:
    found = []
    for key, spec in LABELS.items():
        for phrase in spec.sublabels if english else spec.phrases:
            if english and len(phrase) < 8:
                match = re.search(r"\b" + re.escape(phrase) + r"\b", text)
                if match:
                    found.append(LabelSpan(key, match.start(), match.end()))
                continue
            if len(text) < max(4, 0.6 * len(phrase)):
                continue
            match = fuzz.partial_ratio_alignment(
                phrase, text, score_cutoff=settings.label_fuzzy_cutoff
            )
            if match and (not english or match.dest_end - match.dest_start >= 0.7 * len(phrase)):
                found.append(LabelSpan(key, match.dest_start, match.dest_end))
    return found


def _merge_spans(spans: list[LabelSpan]) -> list[LabelSpan]:
    result: list[LabelSpan] = []
    for key in LABELS:
        for span in sorted(
            (span for span in spans if span.key == key), key=lambda item: item.start
        ):
            if result and result[-1].key == key and span.start <= result[-1].end:
                result[-1] = LabelSpan(key, result[-1].start, max(result[-1].end, span.end))
            else:
                result.append(span)
    return sorted(result, key=lambda item: (item.start, item.end))


def _append_value(
    value: str,
    confidence: float,
    key: str,
    settings: Settings,
    debug: ParseDebug | None,
    values: list[str],
    confidences: list[float],
) -> None:
    if key in {"full_name", "origin", "address"}:
        found: list[str] = []
        value = scrub_labels(value, settings, found)
        if debug is not None:
            debug.scrubbed.extend((key, text) for text in found)
        if is_label_only(value, settings):
            return
    if sum(char.isalnum() for char in value) >= 2:
        values.append(value)
        confidences.append(confidence)


def compute_columns(
    lines: Sequence[OcrLine], settings: Settings
) -> tuple[float | None, float | None]:
    anchors = [
        line.x1
        for line in lines
        if any(
            span.key in RIGHT_FIELDS and span.start <= 3
            for span in _find_spans(fold_aligned(line.text), english=False, settings=settings)
        )
    ]
    # Tọa độ đồng nhất không cung cấp bằng chứng có hai cột (fixture một cột cũ).
    if len(anchors) < settings.cccd_min_anchor_lines or len({line.x1 for line in lines}) <= 1:
        return None, None
    right = median(anchors)
    height = median(line.y2 - line.y1 for line in lines)
    return right, right - settings.cccd_col_split_ratio * height


def _collect(
    lines: Sequence[OcrLine], settings: Settings, debug: ParseDebug | None
) -> tuple[dict[str, tuple[str, float, str]], bool]:
    x_right, x_split = compute_columns(lines, settings)

    def split_offset(text: str) -> int | None:
        aligned = fold_aligned(text)
        labels = _find_spans(aligned, english=False, settings=settings) + _find_spans(
            aligned, english=True, settings=settings
        )
        return min((span.start for span in labels if span.key in RIGHT_FIELDS), default=None)

    fragments = build_fragments(lines, x_split=x_split, x_right=x_right, split_offset=split_offset)
    vietnamese = [
        _find_spans(fold_aligned(item.text), english=False, settings=settings) for item in fragments
    ]
    english = [
        _find_spans(fold_aligned(item.text), english=True, settings=settings) for item in fragments
    ]
    has_primary = {span.key for spans in vietnamese for span in spans}
    spans = [
        _merge_spans(vi + [span for span in en if span.key not in has_primary])
        for vi, en in zip(vietnamese, english, strict=True)
    ]
    boundaries = [vi + en for vi, en in zip(vietnamese, english, strict=True)]
    height = median(line.y2 - line.y1 for line in lines) if lines else 0
    values: dict[str, list[str]] = {}
    confidences: dict[str, list[float]] = {}
    labels: dict[str, tuple[str, float]] = {}
    used: dict[str, list[int]] = {}
    owners: dict[int, str] = {}

    def take(key: str, index: int, text: str) -> bool:
        if index in owners:
            return False
        before = len(values[key])
        _append_value(
            text, fragments[index].conf, key, settings, debug, values[key], confidences[key]
        )
        if len(values[key]) == before:
            return False
        owners[index] = key
        used.setdefault(key, []).append(index)
        return True

    passes = (
        (("right", RIGHT_FIELDS), ("left", LEFT_FIELDS))
        if x_split is not None
        else (("right", frozenset(LABELS)),)
    )
    for column, allowed in passes:
        current: str | None = None
        taken = 0
        last_y2 = 0.0
        for index, item in enumerate(fragments):
            if item.column != column:
                continue
            tags = [tag for tag in spans[index] if tag.key in allowed]
            own_boundaries = [tag for tag in boundaries[index] if tag.key in allowed]
            # Nhãn Anh của chính trường đang mở có thể kèm giá trị (vd. Date of expiry: ngày).
            if (
                not tags
                and current is not None
                and own_boundaries
                and all(tag.key == current for tag in own_boundaries)
            ):
                tags = _merge_spans([tag for tag in english[index] if tag.key == current])
            if tags:
                # Mỗi nhãn kết thúc trường trước, kể cả khi không lấy được giá trị cùng dòng.
                current = None
                for position, tag in enumerate(tags):
                    values.setdefault(tag.key, [])
                    confidences.setdefault(tag.key, [])
                    labels.setdefault(tag.key, (item.text, item.conf))
                    end = tags[position + 1].start if position + 1 < len(tags) else len(item.text)
                    inline = item.text[tag.end : end].lstrip(": /.-|\t").strip()
                    if tag.key not in {"full_name", "origin", "address"}:
                        al = fold_aligned(inline)
                        for sub in LABELS[tag.key].sublabels:
                            if al.startswith(sub):
                                inline = inline[len(sub) :].lstrip(": /.-|\t").strip()
                                break
                    if not values[tag.key] or tag.key in MULTILINE_FIELDS:
                        take(tag.key, index, inline)
                    current = tag.key
                taken = len(values[current])
                last_y2 = item.y2
                continue
            if boundaries[index]:
                # Nhãn cột kia không được nhận làm giá trị; nhãn khác cùng cột đóng trường hiện tại.
                if own_boundaries:
                    current = None
                continue
            # Giá trị giới tính "Nữ" chỉ có hai chữ-số nhưng không phải nhãn phụ.
            if current is None or (current != "gender" and is_label_only(item.text, settings)):
                continue
            if (
                item.y1 - last_y2 > settings.field_gap_ratio * height
                or (current not in MULTILINE_FIELDS and taken >= 1)
                or (current in MULTILINE_FIELDS and taken >= settings.cccd_multiline_max)
            ):
                current = None
                continue
            if take(current, index, item.text.strip()):
                taken += 1
                last_y2 = item.y2
    if debug is not None:
        debug.x_right, debug.x_split = x_right, x_split
        debug.fragments = fragments
        debug.used_fragments = {key: tuple(indices) for key, indices in used.items()}
        debug.label_splits = sorted({item.source for item in fragments if item.estimated})
    captured = {
        key: (
            "\n".join(value),
            min(confidences[key]) if confidences[key] else labels[key][1],
            labels[key][0],
        )
        for key, value in values.items()
    }
    return captured, x_split is not None


def parse_cccd_front(
    lines: Sequence[OcrLine], settings: Settings, today: date, debug: ParseDebug | None = None
) -> CccdFront:
    captured, layout_reliable = _collect(lines, settings, debug)
    fields: dict[str, Field] = {}
    no_expiry = False
    for key in ("full_name", "dob", "gender", "origin", "address", "expiry"):
        raw, confidence, label = captured.get(key, ("", None, ""))
        if key == "dob" and not raw and parse_date(label) is not None:
            raw = label
        value: Any = None
        status: Literal["parsed", "missing", "invalid"] = "missing"
        if raw:
            if key == "full_name":
                value = clean_name(raw) or None
            elif key == "dob":
                value = parse_date(raw) or parse_date(label)
                if value and (value.year < 1900 or value > today):
                    value = None
            elif key == "gender":
                value = parse_gender(raw)
            elif key in MULTILINE_FIELDS:
                value = clean_address(raw.splitlines()) or None
            elif key == "expiry":
                no_expiry = is_no_expiry(raw)
                value = None if no_expiry else parse_date(raw)
                dob = fields["dob"].value
                if value and dob and value <= dob:
                    value = None
            status = "parsed" if value is not None or (key == "expiry" and no_expiry) else "invalid"
            if key == "gender" and value is None:
                status = "missing"
        fields[key] = Field(raw or None, value, confidence if raw else None, status)
    identifier = extract_id_number(lines)
    id_field = (
        Field(identifier[0], identifier[0], identifier[1], "parsed")
        if identifier
        else Field(None, None, None, "missing")
    )
    return CccdFront(
        id_field,
        fields["full_name"],
        fields["dob"],
        fields["gender"],
        fields["origin"],
        fields["address"],
        fields["expiry"],
        no_expiry,
        layout_reliable,
    )


def parse_cccd_back(lines: Sequence[OcrLine], settings: Settings) -> CccdBack:
    for line in sorted(lines, key=lambda item: (item.center_y, item.x1)):
        value = parse_date(line.text)
        if value:
            return CccdBack(Field(line.text, value, line.conf, "parsed"))
    return CccdBack(Field(None, None, None, "missing"))
