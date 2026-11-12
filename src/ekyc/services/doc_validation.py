from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.doc_types import DocClassification, DocType, Side


def ensure_document(
    result: DocClassification, *, expected_type: DocType, expected_side: Side | None = None
) -> None:
    if not result.readable:
        raise EkycError(ErrorCode.UNREADABLE_DOCUMENT)
    details = [{"expected": expected_type.value, "detected": result.doc_type.value}]
    if result.doc_type == DocType.CMND:
        raise EkycError(
            ErrorCode.WRONG_DOCUMENT_TYPE,
            "Hệ thống chưa hỗ trợ chứng minh nhân dân (CMND), "
            "vui lòng dùng giấy tờ đúng loại yêu cầu.",
            details=details,
        )
    if result.doc_type != expected_type:
        raise EkycError(ErrorCode.WRONG_DOCUMENT_TYPE, details=details)
    if result.side == Side.UNKNOWN and expected_side is not None:
        raise EkycError(ErrorCode.UNREADABLE_DOCUMENT)
    if expected_side is not None and result.side != expected_side:
        raise EkycError(
            ErrorCode.WRONG_SIDE,
            details=[
                {
                    "expected": expected_side.value,
                    "detected": result.side.value,
                }
            ],
        )
