import logging
import sys
import traceback
from contextvars import ContextVar

request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_ctx.get()
        return True


class SafeFormatter(logging.Formatter):
    def formatException(self, exc_info: tuple) -> str:
        # Giữ toàn bộ khung stack, bỏ nội dung lỗi và dòng mã có thể chứa dữ liệu riêng tư.
        exc_type, _, tb = exc_info
        frames = traceback.extract_tb(tb)
        stack = " | ".join(f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in frames)
        return f"Traceback: {stack} | {exc_type.__name__}"

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace("\r", "\\r").replace("\n", "\\n")


def setup_logging(level: str) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    # HTTP client có thể ghi URL ký ở INFO; chỉ giữ cảnh báo kỹ thuật.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(SafeFormatter("%(levelname)s request_id=%(request_id)s %(message)s"))
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)


def mask_id_number(value: str) -> str:
    if len(value) < 7:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]
