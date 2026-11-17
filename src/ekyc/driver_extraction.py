"""Local extraction adapter for the driver demo."""

import json
import sys
from datetime import date

from ekyc.config import Settings
from ekyc.core.errors import EkycError
from ekyc.schemas.document import ExtractionResult
from ekyc.services.document_extraction import (
    DocumentExtractor,
    expiry_status,
    legacy_values,
    ocr_status,
    record_conflicts,
    same_field_value,
)
from ekyc.services.document_extraction import (
    parse_qr as parse_qr,
)
from ekyc.services.document_extraction import (
    read_oriented as read_oriented,
)

SLOTS = {"cccd_front", "cccd_back", "gplx_front", "gplx_back"}


def parse_frame(frame, slot, engine, cfg, diagnostics=None):
    extraction = DocumentExtractor(cfg, engine=engine)
    raw, lines = extraction.read_text(frame)
    parsed = extraction.parse(slot, lines, date.today())
    if diagnostics is not None and slot == "gplx_front":
        diagnostics["gplx_expiry"] = expiry_status([*raw, *lines], parsed, cfg)
    candidates = extraction.candidates(slot, parsed)
    return ocr_status(raw, candidates), legacy_values(candidates, slot, "ocr")


def worker(data, slot, mode, diagnostics=None, structured=None):
    cfg = Settings(_env_file=None, ekyc_api_key="driver-demo")
    extractor = DocumentExtractor(cfg, engine_factory=demo_engine)
    try:
        result = extractor.extract(data, slot, qr=mode == "qr", ocr=mode == "ocr")
    except EkycError:
        return ("OCR_FAILED" if mode == "ocr" else "QR_DECODE_FAILED"), {}
    if diagnostics is not None:
        diagnostics.update(result.field_statuses)
    if structured is not None:
        structured.update(result.model_dump(mode="json"))
    status = result.ocr_status if mode == "ocr" else result.qr_status
    return status, legacy_values(result.candidates, slot, mode)


def demo_engine(cfg):
    import torch

    from ekyc.pipeline.ocr.easyocr_engine import EasyOcrEngine

    torch.set_num_threads(4)
    return EasyOcrEngine(cfg.ocr_languages, width_ths=cfg.ocr_width_ths, download_enabled=False)


def source_observation(result, source):
    return {
        "status": result.ocr_status if source == "ocr" else result.qr_status,
        "values": legacy_values(result.candidates, result.slot, source),
        "field_statuses": result.field_statuses,
        "extraction": result.model_dump(mode="json"),
    }


def merge_observations(slot, qr, ocr):
    result = ExtractionResult(
        slot=slot,
        qr_status=qr["status"],
        ocr_status=ocr["status"],
        field_statuses=ocr.get("field_statuses", {}),
    )
    for observation in (qr, ocr):
        if data := observation.get("extraction"):
            partial = ExtractionResult.model_validate(data)
            result.candidates.extend(partial.candidates)
            result.warnings.extend(partial.warnings)
    record_conflicts(result)
    return result.model_dump(mode="json")


def suggestions(observations):
    candidates = {}
    for row in observations:
        for source in ("qr", "ocr"):
            for key, value in row[source]["values"].items():
                values = candidates.setdefault(key, {"qr": [], "ocr": []})[source]
                if not any(same_field_value(key, value, prior) for prior in values):
                    values.append(value)
    result = {}
    for key, sources in candidates.items():
        unique = []
        for value in sources["qr"] + sources["ocr"]:
            if not any(same_field_value(key, value, prior) for prior in unique):
                unique.append(value)
        conflict = len(unique) > 1
        result[key] = {
            "qr_value": sources["qr"][0] if len(sources["qr"]) == 1 else None,
            "ocr_value": sources["ocr"][0] if len(sources["ocr"]) == 1 else None,
            "qr_options": sources["qr"],
            "ocr_options": sources["ocr"],
            "suggested_value": unique[0] if not conflict else None,
            "extraction_status": "CONFLICT" if conflict else "SUGGESTED",
            "warning_codes": ["QR_OCR_MISMATCH"] if conflict else [],
        }
    return result


if __name__ == "__main__":
    mode, slot = sys.argv[1:]
    diagnostics = {}
    structured = {}
    try:
        status, values = worker(sys.stdin.buffer.read(), slot, mode, diagnostics, structured)
    except Exception:
        status, values = ("OCR_FAILED" if mode == "ocr" else "QR_DECODE_FAILED"), {}
        diagnostics.clear()
    print(
        json.dumps(
            {
                "status": status,
                "values": values,
                "field_statuses": diagnostics,
                "extraction": structured,
            }
        )
    )
