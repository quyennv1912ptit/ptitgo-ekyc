import pytest

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.doc_types import DocClassification, DocType, Side
from ekyc.services.doc_validation import ensure_document


def result(doc_type: DocType, side: Side = Side.FRONT, readable: bool = True) -> DocClassification:
    return DocClassification(doc_type, side, 1, 0, [], readable)


@pytest.mark.parametrize(
    "classification,side,code,details",
    [
        (result(DocType.CMND, readable=False), Side.FRONT, ErrorCode.UNREADABLE_DOCUMENT, None),
        (
            result(DocType.CMND),
            Side.FRONT,
            ErrorCode.WRONG_DOCUMENT_TYPE,
            [{"expected": "CCCD", "detected": "CMND"}],
        ),
        (
            result(DocType.GPLX),
            Side.FRONT,
            ErrorCode.WRONG_DOCUMENT_TYPE,
            [{"expected": "CCCD", "detected": "GPLX"}],
        ),
        (
            result(DocType.UNKNOWN),
            Side.FRONT,
            ErrorCode.WRONG_DOCUMENT_TYPE,
            [{"expected": "CCCD", "detected": "UNKNOWN"}],
        ),
        (result(DocType.CCCD, Side.UNKNOWN), Side.FRONT, ErrorCode.UNREADABLE_DOCUMENT, None),
        (
            result(DocType.CCCD, Side.BACK),
            Side.FRONT,
            ErrorCode.WRONG_SIDE,
            [{"expected": "FRONT", "detected": "BACK"}],
        ),
    ],
)
def test_validation(
    classification: DocClassification, side: Side, code: ErrorCode, details: list[dict] | None
) -> None:
    with pytest.raises(EkycError) as info:
        ensure_document(classification, expected_type=DocType.CCCD, expected_side=side)
    assert info.value.code == code
    assert info.value.details == details
    if classification.doc_type == DocType.CMND and classification.readable:
        assert info.value.message == (
            "Hệ thống chưa hỗ trợ chứng minh nhân dân (CMND), "
            "vui lòng dùng giấy tờ đúng loại yêu cầu."
        )


def test_valid() -> None:
    assert (
        ensure_document(result(DocType.CCCD), expected_type=DocType.CCCD, expected_side=Side.FRONT)
        is None
    )
    assert ensure_document(result(DocType.CCCD, Side.UNKNOWN), expected_type=DocType.CCCD) is None
    assert ensure_document(result(DocType.CCCD, Side.BACK), expected_type=DocType.CCCD) is None


def test_cmnd_always_unsupported() -> None:
    with pytest.raises(EkycError) as info:
        ensure_document(result(DocType.CMND), expected_type=DocType.CMND)
    assert info.value.code == ErrorCode.WRONG_DOCUMENT_TYPE
