import unicodedata

from rapidfuzz import fuzz

from ekyc.config import Settings
from ekyc.pipeline.normalize import fold_aligned
from ekyc.pipeline.parsers.cccd_labels import ENGLISH_SCRUB_LABELS

ADJACENT = ": . / - |\t\r\n"
OUTER = ADJACENT + ","


def scrub_labels(text: str, settings: Settings, found: list[str] | None = None) -> str:
    out = unicodedata.normalize("NFC", text)
    for _ in range(4):
        aligned = fold_aligned(out)
        best = None
        for label in ENGLISH_SCRUB_LABELS:
            match = fuzz.partial_ratio_alignment(
                label, aligned, score_cutoff=settings.scrub_fuzzy_cutoff
            )
            if match and match.dest_end - match.dest_start >= 0.7 * len(label):
                if best is None or match.score > best.score:
                    best = match
        if best is None:
            break
        start, end = best.dest_start, best.dest_end
        while start > 0 and (out[start - 1].isspace() or out[start - 1] in ADJACENT):
            start -= 1
        while end < len(out) and (out[end].isspace() or out[end] in ADJACENT):
            end += 1
        if found is not None:
            found.append(out[start:end])
        out = out[:start] + out[end:]
    start, end = 0, len(out)
    while start < end and (out[start].isspace() or out[start] in OUTER):
        start += 1
    while end > start and (out[end - 1].isspace() or out[end - 1] in OUTER):
        end -= 1
    return out[start:end]


def is_label_only(text: str, settings: Settings) -> bool:
    return sum(char.isalnum() for char in scrub_labels(text, settings)) < 3
