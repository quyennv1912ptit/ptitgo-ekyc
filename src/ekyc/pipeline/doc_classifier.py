import logging
import math
from collections.abc import Sequence

from rapidfuzz import fuzz

from ekyc.config import Settings
from ekyc.pipeline.doc_rules import Feature, build_rules
from ekyc.pipeline.doc_types import DocClassification, DocType, Side
from ekyc.pipeline.normalize import fold
from ekyc.pipeline.ocr.base import OcrLine

logger = logging.getLogger(__name__)


class DocClassifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings.model_copy(deep=True)
        self.rules = build_rules(self.settings)

    def _matches(self, feature: Feature, texts: list[tuple[str, float]]) -> bool:
        for text, y in texts:
            if feature.top_only and y > self.settings.doc_top_fraction:
                continue
            if feature.pattern is not None and feature.pattern.search(text):
                return True
            if any(
                len(text) >= max(6, 0.6 * len(phrase))
                and fuzz.partial_ratio(phrase, text, score_cutoff=self.settings.doc_fuzzy_cutoff)
                != 0
                for phrase in feature.phrases
            ):
                return True
        return False

    def classify(self, lines: Sequence[OcrLine], img_height: float) -> DocClassification:
        if not math.isfinite(img_height) or img_height <= 0:
            raise ValueError("img_height must be finite and positive")
        texts = [
            (text, line.center_y / img_height)
            for line in lines
            if line.conf >= self.settings.doc_min_line_conf and (text := fold(line.text))
        ]
        readable = sum(len(text) for text, _ in texts) >= self.settings.doc_min_text_chars
        if not readable:
            return self._log(DocClassification(DocType.UNKNOWN, Side.UNKNOWN, 0.0, 0.0, [], False))
        scored = []
        for rule in self.rules:
            matched = [feature for feature in rule.features if self._matches(feature, texts)]
            score = min(1.0, sum(feature.weight for feature in matched))
            evidence = [f"{rule.key}:{feature.id}" for feature in matched]
            scored.append((rule, score, evidence))
        best, score, evidence = max(scored, key=lambda item: item[1])
        runner_up = max(
            (value for rule, value, _ in scored if rule.doc_type != best.doc_type), default=0.0
        )
        doc_type, side = DocType.UNKNOWN, Side.UNKNOWN
        if (
            score >= self.settings.doc_accept_score
            and score - runner_up >= self.settings.doc_margin
        ):
            doc_type = best.doc_type
            other_side = max(
                (
                    value
                    for rule, value, _ in scored
                    if rule.doc_type == best.doc_type and rule.side != best.side
                ),
                default=0.0,
            )
            if score - other_side >= self.settings.doc_side_margin:
                side = best.side
        return self._log(DocClassification(doc_type, side, score, runner_up, evidence, True))

    @staticmethod
    def _log(result: DocClassification) -> DocClassification:
        logger.debug(
            "doc_type=%s side=%s score=%s evidence=%s",
            result.doc_type.value,
            result.side.value,
            result.score,
            result.evidence,
        )
        return result
