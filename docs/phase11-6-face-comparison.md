# Phase 11.6 — Đối chiếu khuôn mặt và cảnh báo xét duyệt

**Trạng thái** `OFF / INSUFFICIENT_EVIDENCE`. Giữ cấu hình mặc định `FACE_COMPARISON_ENABLED=false` và giới hạn triển khai của Phase 10.8. Không đưa model, dữ liệu mẫu khuôn mặt hoặc ngưỡng mới vào hệ thống.

## Adapter và kết quả

Đường xử lý dùng lại adapter OpenCV local của Phase 10.8 trong worker có deadline. Manifest đã có đường dẫn artifact cục bộ, SHA-256, provenance và quality profile. Card preparer nắn ảnh CCCD/GPLX trước khi tìm mặt, selfie dùng ảnh đã chuẩn hóa hướng. Dò đúng một ứng viên và kiểm tra kích thước, biên mặt, các điểm khuôn mặt, mờ, tối và lóa. Hệ thống thiếu detector che khuất độc lập và tập đánh giá riêng cho chân dung giấy tờ Việt Nam.

Ba cặp gồm CCCD–selfie, GPLX–selfie và CCCD–GPLX. Ảnh thiếu dẫn đến `NOT_EVALUATED`. Ảnh không qua kiểm tra chất lượng dẫn đến `INSUFFICIENT_QUALITY`. Cặp đạt chất lượng vẫn `INCONCLUSIVE`, kể cả điểm cosine rất cao. Nếu kích thước hai vector không khớp, cặp vẫn `INCONCLUSIVE` và không có điểm. `POSSIBLE_MISMATCH` và `SIMILARITY_SUPPORTED` giữ trong hợp đồng cho policy đã được đánh giá sau này. `thresholdVersion` chưa có giá trị và liveness luôn `NOT_EVALUATED`.

## Driver, admin và dữ liệu

- Driver chỉ thấy lựa chọn khi server cho phép thử nghiệm. Sau khi tài xế đồng ý và chủ động bấm kiểm tra, endpoint owner-only `POST /ekyc/driver-verification/v1/applications/{id}/face-quality` dùng ảnh đã tải ở revision đang sửa. Gọi detector và quality gate, bỏ qua trích vector và trả cảnh báo Phase 11.1 cạnh ảnh tương ứng. Có thể chụp lại hoặc tiếp tục gửi để admin xem thủ công. Kết quả nháp dùng trong phiên, không đi vào snapshot bản nộp. Sau khi ảnh hoặc revision thay đổi, kết quả nháp trước không còn hiệu lực.
- goAdmin đối chiếu trên bản nộp hiện tại có consent qua endpoint admin có sẵn. Cảnh báo về chất lượng hiện cạnh đúng ảnh khi có kết quả và được trả lại khi tải hồ sơ. Driver có thể xem cảnh báo của bản nộp. Khi cần sửa ảnh đã nộp, admin dùng luồng yêu cầu bổ sung hiện tại.
- Server kiểm tra quyền, revision và consent trước khi mở ảnh, rồi kiểm tra lại revision sau khi worker trả về. Dùng chung giới hạn một face job đang chạy. Khi flag tắt hoặc thiếu artifact hợp lệ, kết quả giữ `NOT_EVALUATED`.
- Chỉ lưu mã chất lượng đã cho phép và trạng thái cặp từ lượt admin thành công. Các bản ghi cũ chỉ chứa danh sách trạng thái vẫn đọc được. Nhật ký access ghi người thực hiện, thao tác, thời điểm và revision. Không lưu vector, ảnh crop hoặc điểm cosine. Điểm thử nghiệm chỉ trả cho admin của bản nộp có quyền, với `Cache-Control: no-store`. API nháp không trả điểm. Worker không gửi ảnh đến bên ngoài.
- Cảnh báo face không chặn nộp, không chặn kích hoạt và không cấp/thu hồi quyền nhận chuyến. Quyết định xác minh tiếp tục do admin thực hiện. Đối chiếu ảnh tĩnh không chứng minh người thật hoặc giấy tờ thật.

## Kiểm thử và điều kiện sử dụng

Dùng ảnh checkerboard tổng hợp và mock engine để kiểm tra ba cặp, các tình huống không có mặt/có nhiều mặt, mặt bị cắt, lóa, mờ và trạng thái abstain. Ca API kiểm tra quyền owner/admin, consent, bỏ response khi revision thay đổi, luồng mặc định OFF, và cảnh báo cùng vị trí ảnh trong hồ sơ. Các kiểm tra này xác minh luồng phần mềm. Chưa đo độ chính xác nhận dạng và chưa chạy thử trên Android thật.

- eKYC qua Ruff và 466 test Pytest.
- Expo monorepo qua `npm run check`, gồm kiểm tra kiến trúc, lint, TypeScript và 176 test.
- goAdmin qua lint, TypeScript và 7 test trang xét duyệt. Regression test kiểm tra kết quả đối chiếu vẫn hiển thị khi đưa cảnh báo mới vào bên cạnh ảnh.

Muốn đánh giá để bật phân loại sau này cần artifact được phép sử dụng, dữ liệu đã có quyền xử lý, giao thức đo FAR/FRR theo nhóm ảnh và ngưỡng được khóa sau đánh giá. Giữ feature OFF khi chưa có bằng chứng. Không deploy và dừng trước Phase 11.7.


## Bật đối chiếu theo yêu cầu vận hành

Cấu hình triển khai hiện cho phép đối chiếu có sự đồng ý của tài xế ở staging và production.

- Backend dùng `DATXE_EKYC_FACE_COMPARISON_ENABLED=true`. Môi trường local đã bật cờ này.
- Workflow Modal dùng biến `MODAL_FACE_COMPARISON_ENABLED`, mặc định `true` khi deploy qua workflow. Đặt `false` để tắt.
- Khi bật, Modal dùng `/opt/ekyc/face/manifest.json` và cần hai model YuNet/SFace cùng manifest tại thư mục `face/` trong model volume. Pre-deploy kiểm tra các file trước khi triển khai. Worker kiểm tra SHA-256 khi nạp model.
- Nếu deploy với nguồn model local, entrypoint đóng gói manifest, model và giấy phép từ `.local-face/`.
- Bản nộp phải chứa `faceConsent.version = face-comparison-v1`. Với bản nộp cũ thiếu đồng ý, tài xế cần xác nhận và nộp lại qua app. Admin mở bản nộp hiện tại và bấm **Chạy đối chiếu thử nghiệm**.
- Kết quả vẫn là điểm cosine hỗ trợ admin đối chiếu ảnh. Ngưỡng kết luận và kiểm tra người thật vẫn ở trạng thái chưa đánh giá.
