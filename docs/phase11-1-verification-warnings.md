# Phase 11.1 — Cảnh báo xác minh giấy tờ

eKYC tính cảnh báo từ ảnh và lượt trích xuất hiện tại, dữ liệu tài xế đã xác nhận, và trạng thái đối chiếu khuôn mặt nếu tính năng được bật. Expo Driver và goAdmin đọc cùng một cấu trúc từ API xác minh hiện có. Backend đặt xe dùng dữ liệu đủ điều kiện đã lọc từ luồng đồng bộ Phase 11.0.

## Hợp đồng và kiểm soát theo revision

- `verificationWarnings` trả mã, mức độ, loại giấy tờ, vị trí ảnh, trường liên quan, thông điệp, hướng xử lý, nguồn bằng chứng, revision và ba cờ `requiresReview`, `blocksSubmission`, `blocksActivation`. Mức độ gồm `INFO`, `WARNING`, `CRITICAL`, `BLOCKING`.
- Thay ảnh làm mất hiệu lực lượt OCR gắn với ảnh trước. Khi tài xế lưu lại thông tin, backend tính lại cảnh báo từ revision mới. So sánh họ tên và ngày sinh riêng. Họ tên khác dấu vẫn cần đối chiếu thủ công.
- Ảnh bắt buộc còn thiếu chặn nộp. Các cảnh báo cần xem lại vẫn cho nộp khi tài xế gửi `warningAcknowledgementRevision` bằng revision đang xem. Gửi thiếu hoặc sai revision trả `WARNING_ACK_REQUIRED`. Bản nộp lưu cảnh báo, mã đã xác nhận và revision đó. Gửi lại cùng idempotency key chỉ hợp lệ với cùng revision và cùng dữ liệu xác nhận cảnh báo.
- Driver có thể xem cảnh báo cạnh ảnh hoặc trường liên quan, rồi sửa hoặc chụp lại. goAdmin hiển thị mã, mức độ và cách xử lý từ cùng API. Workflow yêu cầu bổ sung vẫn dùng danh sách ảnh và trường của bản nộp hiện tại.

## Hiệu lực giấy tờ và quyền nhận chuyến

- Ngày tham chiếu là `Asia/Ho_Chi_Minh`. Giấy tờ còn hiệu lực trong ngày hết hạn đã ghi. GPLX thiếu dữ liệu hạn mà chưa được xác nhận không thời hạn được phân loại chưa xác định.
- eKYC chặn phê duyệt nếu có lỗi kích hoạt đã xác định, gồm giấy tờ quá hạn, hạng GPLX không hợp lệ hoặc có bằng chứng chưa đọc được hạn chế GPLX. Admin có thể yêu cầu bổ sung. Áp dụng cho quyết định API và danh sách quyền được phát bởi luồng đồng bộ.
- Ngày hết hạn CCCD là trường nhập tùy chọn. Khi tài xế đã xác nhận ngày, sự kiện phê duyệt đồng bộ thêm `cccdExpiresOn`. Migration Flyway `V29` thêm cột ngày vào projection. Cổng nhận chuyến từ chối CCCD đã hết hạn ngay cả khi trạng thái xét duyệt vẫn là APPROVED. Bản ghi cũ không có ngày tiếp tục dùng chính sách lịch sử. Cần kiểm tra lại các hồ sơ cũ nếu nghiệp vụ yêu cầu xác minh thời hạn CCCD của chúng.
- Khi chưa nhập hạn CCCD, dữ liệu OCR có ngày quá hạn, qua kiểm tra và không mâu thuẫn có thể chặn duyệt để admin yêu cầu xác minh giấy tờ. Chưa đọc được hạn CCCD chỉ là thông tin để đối chiếu. Stage GPLX hiện chưa xuất trạng thái hạn chế, nên `GPLX_RESTRICTION_UNKNOWN` chỉ sinh khi đầu vào có bằng chứng rõ ràng về tình trạng chưa đọc được, không suy diễn từ trường vắng mặt.
- Face mặc định tắt và trả `FACE_NOT_EVALUATED`. Khi có lượt đối chiếu được phép, lưu trạng thái cặp theo bản nộp để tạo cảnh báo tư vấn. Giữ kết quả khuôn mặt ngoài quyết định kích hoạt và không lưu thêm similarity score hay embedding cho hệ thống cảnh báo.

## Kiểm thử và bàn giao

- eKYC đạt Ruff và 463 test Pytest với ảnh và thông tin tổng hợp. Regression bao gồm từ chối xác nhận cũ hoặc thiếu, submit/resubmit, loại bằng chứng OCR của ảnh cũ, và chặn duyệt khi CCCD hết hạn.
- Expo đạt architecture checks, typecheck, lint và 173 test. Test của màn hình kiểm tra revision của thao tác xác nhận và kết nối nút xác nhận với payload nộp. Kiểm tra giao diện native trực tiếp trên Android vẫn cần thực hiện.
- goAdmin đạt typecheck, lint và 5 test của màn hình xét duyệt với timeout test runner 60 giây mỗi ca. Ca cảnh báo nghiêm trọng xác nhận admin vẫn có thể yêu cầu bổ sung.
- Backend đặt xe đạt Gradle test và bootJar. Kiểm thử projection và migration V29 trên PostgreSQL test tạm đã qua. Kiểm tra code thực hiện khi bật cơ chế đồng bộ Phase 11.0.
- Khi đưa lên staging, cập nhật Expo Driver cùng eKYC hoặc cập nhật app trước. App cũ dùng cùng endpoint nhưng không nộp được hồ sơ có cảnh báo cần xác nhận. Trên Android dùng fixture tổng hợp, kiểm tra đổi ảnh sau khi OCR, thay trường sau khi xem cảnh báo, và gửi lại sau yêu cầu bổ sung. Trên goAdmin kiểm tra bản nộp cũ vẫn xem được và hồ sơ hết hạn chỉ có lựa chọn từ chối hoặc yêu cầu bổ sung.

Dừng tại Phase 11.1. Model, ngưỡng và hiệu năng OCR giữ theo cấu hình hiện có.
