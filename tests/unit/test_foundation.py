import logging
from io import StringIO
from threading import BoundedSemaphore

import pytest

from ekyc.config import Settings
from ekyc.core.concurrency import job_slot
from ekyc.core.errors import ERROR_META, EkycError, ErrorCode, WarningCode
from ekyc.core.logging import RequestIdFilter, SafeFormatter, mask_id_number, request_id_ctx
from ekyc.schemas.common import ApiResponse, CamelModel, Warning


def test_error_meta_complete() -> None:
    assert set(ERROR_META) == set(ErrorCode)
    for code, meta in ERROR_META.items():
        error = EkycError(code)
        assert error.http_status == meta.http_status
        assert error.retryable == meta.retryable
        assert error.message == meta.default_message
        assert meta.default_message
        if meta.http_status == 200:
            assert meta.retryable is False


def test_hosts(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("ALLOWED_IMAGE_HOSTS=example.com, res.cloudinary.com,\n", encoding="utf-8")
    settings = Settings(_env_file=env, ekyc_api_key="fake")
    assert settings.allowed_image_hosts == ["example.com", "res.cloudinary.com"]
    monkeypatch.setenv("ALLOWED_IMAGE_HOSTS", "one.example, two.example")
    assert Settings(_env_file=None, ekyc_api_key="fake").allowed_image_hosts == [
        "one.example",
        "two.example",
    ]
    assert Settings(
        _env_file=None, ekyc_api_key="fake", allowed_image_hosts=["a"]
    ).allowed_image_hosts == ["a"]


@pytest.mark.parametrize(
    "value,expected",
    [("", ""), ("123456", "******"), ("1234567", "123*567"), ("012345678901", "012******901")],
)
def test_mask(value: str, expected: str) -> None:
    assert mask_id_number(value) == expected


def test_slot() -> None:
    sem = BoundedSemaphore(1)
    with job_slot(sem, timeout=0):
        with pytest.raises(EkycError) as info:
            with job_slot(sem, timeout=0):
                pass
        assert info.value.code == ErrorCode.ENGINE_BUSY
    with pytest.raises(ValueError), job_slot(sem, timeout=0):
        raise ValueError("failure")
    assert sem.acquire(blocking=False)
    sem.release()


def test_camel_schema() -> None:
    class Data(CamelModel):
        full_name: str

    response = ApiResponse.ok(
        Data(fullName="fake"), "id", [Warning(code=WarningCode.NAME_PARTIAL_MATCH)]
    )
    dumped = response.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert dumped["data"] == {"fullName": "fake"}
    assert dumped["warnings"] == [{"code": "NAME_PARTIAL_MATCH"}]


def test_private_stack_logging() -> None:
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(SafeFormatter("request_id=%(request_id)s %(message)s"))
    logger = logging.Logger("privacy-test")
    logger.addHandler(handler)
    token = request_id_ctx.set("test-id")
    try:
        try:
            raise RuntimeError("secret signed-url")
        except RuntimeError:
            logger.exception("unhandled_error")
    finally:
        request_id_ctx.reset(token)
    text = output.getvalue()
    assert "request_id=test-id" in text
    assert "Traceback" in text and "RuntimeError" in text
    assert "secret" not in text
    assert len(text.splitlines()) == 1


def test_license_classes(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("LICENSE_CLASS_CODES=A1, B ,,C\n", encoding="utf-8")
    assert Settings(_env_file=env, ekyc_api_key="fake").license_class_codes == ["A1", "B", "C"]
    monkeypatch.setenv("LICENSE_CLASS_CODES", "A1, B ,,C")
    assert Settings(_env_file=None, ekyc_api_key="fake").license_class_codes == ["A1", "B", "C"]


def test_unreadable_error() -> None:
    error = EkycError(ErrorCode.UNREADABLE_DOCUMENT)
    assert error.http_status == 200 and not error.retryable
    assert error.message == "Không đọc được chữ trên giấy tờ, hãy chụp rõ hơn và đủ sáng."


def test_ocr_languages(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("OCR_LANGUAGES=vi, en,,\n", encoding="utf-8")
    assert Settings(_env_file=env, ekyc_api_key="fake").ocr_languages == ["vi", "en"]
    monkeypatch.setenv("OCR_LANGUAGES", "vi, en,,")
    assert Settings(_env_file=None, ekyc_api_key="fake").ocr_languages == ["vi", "en"]


def test_layout_settings(settings: Settings) -> None:
    assert settings.cccd_col_split_ratio == 2.5 and settings.cccd_min_anchor_lines == 2
    assert settings.ocr_width_ths is None
    assert "cccd_col_tol_left_ratio" not in Settings.model_fields
    assert "cccd_col_tol_right_ratio" not in Settings.model_fields


def test_empty_width_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_WIDTH_THS", "")
    assert Settings(_env_file=None, ekyc_api_key="test-key").ocr_width_ths is None
    monkeypatch.setenv("OCR_WIDTH_THS", "0.2")
    assert Settings(_env_file=None, ekyc_api_key="test-key").ocr_width_ths == 0.2
