from dataclasses import dataclass
from enum import Enum


class ErrorCode(str, Enum):  # noqa: UP042 - Theo hợp đồng đặc tả.
    IMAGE_TOO_LARGE = "IMAGE_TOO_LARGE"
    INVALID_IMAGE = "INVALID_IMAGE"
    IMAGE_BLURRY = "IMAGE_BLURRY"
    IMAGE_GLARE = "IMAGE_GLARE"
    CARD_NOT_FOUND = "CARD_NOT_FOUND"
    CARD_CROPPED = "CARD_CROPPED"
    MULTIPLE_CARDS = "MULTIPLE_CARDS"
    IMAGE_TOO_DARK = "IMAGE_TOO_DARK"
    IMAGE_TOO_SMALL = "IMAGE_TOO_SMALL"
    WRONG_DOCUMENT_TYPE = "WRONG_DOCUMENT_TYPE"
    WRONG_SIDE = "WRONG_SIDE"
    UNREADABLE_DOCUMENT = "UNREADABLE_DOCUMENT"
    DOCUMENT_EXPIRED = "DOCUMENT_EXPIRED"
    FIELD_MISSING = "FIELD_MISSING"
    INVALID_FIELD_FORMAT = "INVALID_FIELD_FORMAT"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    NO_FACE_DETECTED = "NO_FACE_DETECTED"
    MULTIPLE_FACES = "MULTIPLE_FACES"
    FACE_TOO_SMALL = "FACE_TOO_SMALL"
    FACE_MISMATCH = "FACE_MISMATCH"
    INVALID_REQUEST = "INVALID_REQUEST"
    URL_NOT_ALLOWED = "URL_NOT_ALLOWED"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    RATE_LIMITED = "RATE_LIMITED"
    IMAGE_FETCH_FAILED = "IMAGE_FETCH_FAILED"
    ENGINE_BUSY = "ENGINE_BUSY"
    ENGINE_TIMEOUT = "ENGINE_TIMEOUT"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class WarningCode(str, Enum):  # noqa: UP042 - Theo hợp đồng đặc tả.
    LOW_OCR_CONFIDENCE = "LOW_OCR_CONFIDENCE"
    QR_OCR_MISMATCH = "QR_OCR_MISMATCH"
    NAME_PARTIAL_MATCH = "NAME_PARTIAL_MATCH"
    FACE_UNCERTAIN = "FACE_UNCERTAIN"
    LAYOUT_UNCERTAIN = "LAYOUT_UNCERTAIN"
    FIELD_NOT_FOUND = "FIELD_NOT_FOUND"
    ID_STRUCTURE_MISMATCH = "ID_STRUCTURE_MISMATCH"
    LOW_RESOLUTION = "LOW_RESOLUTION"


@dataclass(frozen=True)
class ErrorMeta:
    http_status: int
    retryable: bool
    default_message: str


ERROR_META: dict[ErrorCode, ErrorMeta] = {
    ErrorCode.UNREADABLE_DOCUMENT: ErrorMeta(
        200, False, "Không đọc được chữ trên giấy tờ, hãy chụp rõ hơn và đủ sáng."
    ),
    ErrorCode.IMAGE_TOO_LARGE: ErrorMeta(
        200, False, "Ảnh quá lớn, hãy giảm dung lượng rồi thử lại."
    ),
    ErrorCode.INVALID_IMAGE: ErrorMeta(200, False, "Ảnh không hợp lệ, hãy chọn ảnh khác."),
    ErrorCode.IMAGE_BLURRY: ErrorMeta(200, False, "Ảnh bị mờ, hãy giữ máy chắc tay và chụp lại."),
    ErrorCode.IMAGE_GLARE: ErrorMeta(
        200, False, "Ảnh bị lóa, hãy tránh ánh sáng chiếu trực tiếp vào giấy tờ rồi chụp lại."
    ),
    ErrorCode.CARD_NOT_FOUND: ErrorMeta(
        200, False, "Không tìm thấy giấy tờ, hãy chụp lại rõ toàn bộ thẻ."
    ),
    ErrorCode.CARD_CROPPED: ErrorMeta(200, False, "Giấy tờ bị xén, hãy chụp đủ bốn góc."),
    ErrorCode.MULTIPLE_CARDS: ErrorMeta(
        200, False, "Ảnh có nhiều giấy tờ, hãy chụp riêng từng giấy tờ."
    ),
    ErrorCode.IMAGE_TOO_DARK: ErrorMeta(200, False, "Ảnh quá tối, hãy chụp lại ở nơi đủ sáng."),
    ErrorCode.IMAGE_TOO_SMALL: ErrorMeta(
        200, False, "Ảnh quá nhỏ, hãy chọn ảnh có độ phân giải cao hơn."
    ),
    ErrorCode.WRONG_DOCUMENT_TYPE: ErrorMeta(
        200, False, "Sai loại giấy tờ, hãy chọn đúng giấy tờ được yêu cầu."
    ),
    ErrorCode.WRONG_SIDE: ErrorMeta(200, False, "Sai mặt giấy tờ, hãy chụp đúng mặt được yêu cầu."),
    ErrorCode.DOCUMENT_EXPIRED: ErrorMeta(
        200, False, "Giấy tờ đã hết hạn, hãy dùng giấy tờ còn hiệu lực."
    ),
    ErrorCode.FIELD_MISSING: ErrorMeta(
        200, False, "Thiếu thông tin, hãy chụp lại rõ toàn bộ giấy tờ."
    ),
    ErrorCode.INVALID_FIELD_FORMAT: ErrorMeta(
        200, False, "Thông tin không hợp lệ, hãy kiểm tra và chụp lại."
    ),
    ErrorCode.IDENTITY_MISMATCH: ErrorMeta(
        200, False, "Thông tin không khớp, hãy kiểm tra lại giấy tờ."
    ),
    ErrorCode.NO_FACE_DETECTED: ErrorMeta(
        200, False, "Không thấy khuôn mặt, hãy chụp lại rõ khuôn mặt."
    ),
    ErrorCode.MULTIPLE_FACES: ErrorMeta(
        200, False, "Có nhiều khuôn mặt, hãy chụp riêng một người."
    ),
    ErrorCode.FACE_TOO_SMALL: ErrorMeta(200, False, "Khuôn mặt quá nhỏ, hãy đưa máy lại gần hơn."),
    ErrorCode.FACE_MISMATCH: ErrorMeta(
        200, False, "Khuôn mặt không khớp, hãy kiểm tra và chụp lại."
    ),
    ErrorCode.INVALID_REQUEST: ErrorMeta(
        400, False, "Yêu cầu không hợp lệ, hãy kiểm tra thông tin gửi lên."
    ),
    ErrorCode.URL_NOT_ALLOWED: ErrorMeta(
        400, False, "Đường dẫn ảnh không được phép, hãy chọn ảnh từ nguồn được hỗ trợ."
    ),
    ErrorCode.UNAUTHORIZED: ErrorMeta(
        401, False, "Không có quyền truy cập, hãy kiểm tra khóa API."
    ),
    ErrorCode.NOT_FOUND: ErrorMeta(404, False, "Không tìm thấy chức năng, hãy kiểm tra đường dẫn."),
    ErrorCode.METHOD_NOT_ALLOWED: ErrorMeta(
        405, False, "Phương thức không được hỗ trợ, hãy kiểm tra yêu cầu."
    ),
    ErrorCode.RATE_LIMITED: ErrorMeta(429, True, "Quá nhiều yêu cầu, vui lòng thử lại sau."),
    ErrorCode.IMAGE_FETCH_FAILED: ErrorMeta(502, True, "Không tải được ảnh, vui lòng thử lại sau."),
    ErrorCode.ENGINE_BUSY: ErrorMeta(503, True, "Hệ thống đang bận, vui lòng thử lại sau."),
    ErrorCode.ENGINE_TIMEOUT: ErrorMeta(504, True, "Xử lý quá thời gian, vui lòng thử lại sau."),
    ErrorCode.INTERNAL_ERROR: ErrorMeta(500, True, "Hệ thống gặp sự cố, vui lòng thử lại sau."),
}


class EkycError(Exception):
    def __init__(
        self, code: ErrorCode, message: str | None = None, *, details: list[dict] | None = None
    ) -> None:
        self.code = code
        self.message = ERROR_META[code].default_message if message is None else message
        self.details = details
        super().__init__(self.message)

    @property
    def http_status(self) -> int:
        return ERROR_META[self.code].http_status

    @property
    def retryable(self) -> bool:
        return ERROR_META[self.code].retryable
