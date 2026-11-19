# Phase 11.1.3A — Server-side CCCD QR Extraction and Cross-Validation

## 1. Phạm vi và Audit hiện trạng

Tài liệu mô tả thiết kế và triển khai Phase 11.1.3A trên hai repository `F:\ekyc-service` và `F:\datxebackend` nhằm tối ưu giải mã QR trên ảnh CCCD/Căn cước và hỗ trợ quét QR trực tiếp có kiểm chứng, giảm phụ thuộc OCR nhưng vẫn tái sử dụng pipeline hiện có (không tách service mới, không train model, không đổi threshold inference, không deploy production).

### Audit trước thay đổi
- **`ekyc-service` (`src/ekyc/services/document_extraction.py`)**: `parse_qr` chỉ chấp nhận đúng 7 trường, bác cả bản ghi nếu `address` trống hoặc `gender` rỗng, không xuất `address` và `issue_date`. `decode_qr` chỉ chạy một lượt `cv2.QRCodeDetector().detectAndDecode()` trên ảnh gốc trước khi nắn thẻ; không thử trên ảnh đã nắn (`PreparedCard`), không cắt ROI theo mẫu CCCD 2021 / Căn cước 2024, và không thử biến thể tiền xử lý/quiet-zone.
- **Modal deployment (`scripts/modal_ekyc.py`, `pyproject.toml`)**: Đang chạy `ekyc.main:app` trên CPU 4 core với `opencv-python-headless==5.0.0.93` (đã có `QRCodeDetector` với `detectAndDecode`, `detectAndDecodeMulti`, `detectAndDecodeCurved`). Không thêm `zxing-cpp` hay dependency mới.
- **`datxebackend` (`ekyc/EkycClient.kt`, `driver/verification/DriverVerificationWorkflowService.kt`)**: `EkycClient.extract` gọi `POST /ekyc/documents/{slot}/extract` và lưu nguyên `proposal_json` vào `driver_verification_captures`. Chưa có endpoint nhận payload QR quét trực tiếp (`QR_LIVE_SCAN`), chưa đối chiếu QR-vs-OCR xuyên hai mặt (`cccd_back` QR của Căn cước 2024 vs `cccd_front` OCR), và chưa có chỉ mục tra cứu HMAC cho chống trùng CCCD.

---

## 2. QR Decoding đa tầng (`F:\ekyc-service\src\ekyc\pipeline\qr.py`)

Tái sử dụng `cv2.QRCodeDetector` trong OpenCV hiện có và chia thành 2 pha bên trong `DocumentExtractor.extract`:
1. **Pha 1 (trước `prepare_loaded`, trên ảnh gốc `loaded.bgr`)**: giữ nguyên thứ tự stage `validate -> qr -> prepare -> ocr` và cơ chế `on_partial` khi OCR timeout. Chạy trên ảnh gốc tối đa 2500 px theo các hướng đọc; nếu phát hiện vùng QR (`points is not None`) nhưng chưa giải mã được thì crop quanh `points`, đệm quiet-zone trắng (`24px`), phóng to `INTER_CUBIC` và thử các biến thể tương phản.
2. **Pha 2 (khi Pha 1 chưa đạt `DECODED_VALID`)**:
   - **Khi `prepare_loaded` thành công (ảnh đã nắn landscape)**: thử toàn ảnh nắn kèm quiet-zone (`0°`, `180°`), sau đó thử ROI theo mẫu thẻ (ưu tiên theo `slot`):
     - **CCCD gắn chip mẫu 2021 (`cccd_front`)**: góc trên-phải `(0.66, 0.02, 0.99, 0.48)` và cặp đối xứng 180°.
     - **Căn cước mẫu 2024 (`cccd_back`)**: vùng giữa/trên-phải dưới chip `(0.52, 0.04, 0.99, 0.72)` và cặp đối xứng 180°.
     - Phóng to ROI tối thiểu cạnh ngắn `420px`, đệm quiet-zone `24px`, và thử tối đa 3 biến thể tiền xử lý giới hạn (CLAHE, unsharp sharpen, adaptive Gaussian threshold) có kiểm tra deadline `check()`.
   - **Khi YOLO/warp thất bại (`RAW_FRAME_FALLBACK`) hoặc mode `ocr=False`**: không phụ thuộc YOLO/warp; chạy đa tỷ lệ (`0.5x` cho ảnh lớn / `2x` cho ảnh nhỏ), 4 vùng quadrant giao nhau và các biến thể CLAHE/sharpen có giới hạn.
   - **CCCD cũ không có QR**: trả `NOT_FOUND` (`QR_NOT_FOUND`), pipeline tiếp tục OCR bình thường.

---

## 3. QR Parser (`parse_cccd_qr_payload` & `CccdQrParser`)

- Hỗ trợ đồng thời **CCCD 2021** và **Căn cước 2024** mà **không hardcode 7 trường**:
  - Bố cục 7+ trường (`id | old_cmnd | name | dob | gender | address | issue_date [| ...]`).
  - Bố cục 6+ trường không có `old_cmnd` (`id | name | dob | gender | address | issue_date [| ...]`).
  - Bỏ qua các trường thừa phía sau (như số định danh đã hủy hoặc họ tên cha/mẹ cho công dân dưới 14 tuổi trên Căn cước 2024) và **không lưu** số CMND cũ hay trường cá nhân không cần thiết.
- Hỗ trợ định dạng ngày `DDMMYYYY`, `DD/MM/YYYY`, `DD-MM-YYYY` về chuẩn ISO `YYYY-MM-DD`; chuẩn hóa Unicode `NFC`.
- **Chỉ trích xuất các trường nghiệp vụ cần thiết**: `id_number`, `full_name`, `dob`, `gender`, `issue_date`, `address`.
- **Không mặc định QR có ngày hết hạn** (không bao giờ suy diễn `expiry` từ QR).
- **Không áp dụng điều kiện `issue_date - dob >= 14 năm`**: chỉ kiểm tra `dob` hợp lệ (`>= 1900`, `<= today`) và `issue_date` hợp lệ (`>= dob`, `<= today`) để hỗ trợ Căn cước 2024 cấp cho công dân dưới 14 tuổi.
- Nếu thiếu một phần trường tùy chọn (vd. `gender`, `address`, `issue_date`), vẫn trả `DECODED_VALID`, ghi nhận danh sách `missingFields` và cảnh báo `QR_PARTIAL_FIELDS`; nếu thiếu cả `full_name` lẫn `dob` thì trả `INVALID_FORMAT`.

---

## 4. Response Contract

Cả `ExtractionResult.qr` (`F:\ekyc-service`) và endpoint `/qr-scan` (`F:\ekyc-service`, `F:\datxebackend`) trả cấu trúc thống nhất (giữ nguyên `qrStatus` cũ để tương thích ngược):

- **`status`**: `NOT_FOUND`, `DETECTED_UNREADABLE`, `DECODED_VALID`, `INVALID_FORMAT`, `UNSUPPORTED_FORMAT`, `CONFLICT`, `ENGINE_UNAVAILABLE`.
- **Metadata gắn kèm**: `source` (`QR_IMAGE` | `QR_LIVE_SCAN`), `documentSide` (`FRONT` | `BACK` | `UNKNOWN`), `cardVariant` (`CCCD_2021` | `CAN_CUOC_2024` | `UNKNOWN`), `attachmentId`, `revision`, `boundToCapture`, `missingFields`, `conflictFields`, `crossValidationStatus` (`MATCHED` | `CONFLICT` | `INSUFFICIENT_EVIDENCE` | `NOT_APPLICABLE`), `warnings`.
- Không coi `DECODED_VALID` là xác minh danh tính thành công.

---

## 5. Đối chiếu QR với OCR (`compare_qr_and_ocr_fields` / `DriverVerificationWarnings`)

- Chạy đối chiếu ở cả tầng trích xuất đơn lẻ (`DocumentExtractor`) lẫn tầng hồ sơ xuyên hai mặt (`cccd_front` + `cccd_back` + `qrLiveScan` trong `verification_warnings.py` và `DriverVerificationWarnings.kt`):
  - **`id_number`**: khớp chính xác 12 chữ số ASCII, **tuyệt đối không dùng fuzzy matching**.
  - **`dob` / `issue_date`**: chuẩn hóa về ISO `YYYY-MM-DD` rồi đối chiếu chính xác.
  - **`full_name`**: chuẩn hóa NFC, khoảng trắng, không phân biệt hoa/thường; **giữ nguyên dấu** và phân biệt rõ `QR_OCR_NAME_DIACRITIC_MISMATCH` (`WARNING`, chỉ khác dấu) với `QR_OCR_CONFLICT` (`CRITICAL`, khác nội dung).
  - **Thiếu dữ liệu đối chiếu**: gán `crossValidationStatus = "INSUFFICIENT_EVIDENCE"` và sinh cảnh báo `INFO` `QR_OCR_INSUFFICIENT_EVIDENCE`.
  - **Có mâu thuẫn**: sinh cảnh báo tương ứng, **không âm thầm chọn** một nguồn thay cho nguồn kia.

---

## 6. API quét QR trực tiếp (`qr-scan`)

- **Endpoints (Driver authenticated, owner-only)**:
  - `F:\datxebackend`: `POST /ekyc/driver-verification/v1/applications/{applicationId}/qr-scan` và `POST /api/driver/verification/v1/applications/{applicationId}/qr-scan`.
  - `F:\ekyc-service`: `POST /ekyc/driver-verification/v1/applications/{application_id}/qr-scan`.
- **Kiểm soát chặt chẽ**:
  - Chỉ nhận `revision`, `rawPayload`, `documentSide`, `attachmentId`; `ekyc-service` chặn field lạ (`extra="forbid"`), server tự parse lại `rawPayload` hoàn toàn, không tin field do frontend parse.
  - Kiểm tra `revision` và trạng thái chỉnh sửa (`DRAFT` / `NEEDS_MORE_INFO` + `requestedChanges`), kiểm tra `attachmentId` phải thuộc ảnh CCCD hiện hành chưa bị thay thế (`409 REVISION_CONFLICT` nếu cũ).
  - Gắn `source = "QR_LIVE_SCAN"`, `boundToCapture = False` và phát sinh cảnh báo `QR_LIVE_SCAN_UNBOUND` (`INFO`) vì quét live không chứng minh được QR nằm trên ảnh CCCD đã upload.
  - Khi tài xế thay ảnh `cccd_front` hoặc `cccd_back`, `qrLiveScan` gắn với ảnh/mặt đó tự động bị xóa hiệu lực.

---

## 7. Bảo mật, Privacy & Identity Lock

- Không log và không lưu `rawPayload`, không lưu `old_cmnd`/`canceled_id`/`parent_name`, không log số định danh hay ảnh giấy tờ; bảng audit chỉ lưu `source`, `documentSide`, `attachmentId`, `status`, `hasConflicts`.
- Giữ nguyên toàn bộ **Identity Lock** (`409 IDENTITY_LOCKED` khi đổi số định danh đã khóa) và bổ sung kiểm tra `id_number` của QR với `lockedCccdId` ngay trong `DriverVerificationWarnings` (`LOCKED_CCCD_ID_CHANGED`).
- **Chống trùng bằng HMAC server-side**:
  - `F:\datxebackend`: Migration `V40__qr_live_scan_and_cccd_hmac.sql` thêm `qr_live_scan_json`, `cccd_id_hmac`, `qr_cccd_id_hmac` và index tra cứu trên `driver_verification_applications`; dùng `HmacSHA256` với khóa bí mật `app.verification.lookup-hmac-key` (fallback `app.verification.encryption-key` ở server).
  - `F:\ekyc-service`: `VerificationStore` thêm `cccd_id_hmac` / `qr_cccd_id_hmac` (HMAC-SHA256 với `VERIFICATION_LOOKUP_HMAC_KEY` hoặc `VERIFICATION_STORAGE_KEY`) để phát hiện `DUPLICATE_CCCD_ACROSS_ACCOUNTS` không cần lưu số CCCD thô ở cột tra cứu.

---

## 8. Kiểm thử đã chạy

- **`F:\ekyc-service`**:
  - `uv run ruff check .` -> `All checks passed!`
  - `uv run pytest -q --tb=short` -> **482 passed** (bao gồm `tests/unit/test_qr_pipeline.py` kiểm thử CCCD 2021, Căn cước 2024 dưới 14 tuổi, bố cục 6/7/9 trường, thiếu trường, sai/không hỗ trợ format, QR nghiêng/mờ qua ROI + raw fallback, `DETECTED_UNREADABLE`, `ENGINE_UNAVAILABLE`, QR/OCR mismatch, cross-slot `cccd_back` QR vs `cccd_front` OCR, `qr-scan` revision cũ/unauthorized/extra fields, privacy log và trùng tài khoản qua HMAC).
- **`F:\datxebackend`**:
  - `./gradlew.bat test` -> **BUILD SUCCESSFUL**: 63 test suites, 239 tests chạy (187 passed, 52 env-gated PostgreSQL/Modal smoke tests skipped do không đặt biến môi trường DB/Modal ngoài, 0 failures, 0 errors), bao gồm `CccdQrBackendTest` (**5/5 passed**).
