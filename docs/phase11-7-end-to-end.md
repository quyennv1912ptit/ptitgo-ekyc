# Phase 11.7 — Kiểm thử Customer, Driver và goAdmin

## Môi trường local test

| Thành phần | Thiết bị hoặc địa chỉ | Đã xác minh |
| --- | --- | --- |
| Customer | Pixel_4a, `emulator-5554`, Metro cổng 8081 | Android đã mở ứng dụng và Metro đã gửi bundle. |
| Driver | Android thật V2342, Metro cổng 8082 | Android đã mở ứng dụng, Metro đã gửi bundle và thấy màn hình đăng nhập. |
| Backend đặt xe | `http://127.0.0.1:8080` | Health UP. PostgreSQL `datxe_android_test` và Redis test, migration đến v31. |
| eKYC | `https://localhost:8443` | Health và readiness OK qua CA local. Worker nạp model theo yêu cầu. |
| goAdmin | `http://localhost:5173` | Trang dev trả HTTP 200. |

ADB reverse của Customer chuyển tiếp 8080 và 8081. Driver chuyển tiếp 8080, 8082 và 8443. Cả hai app dùng API đặt xe `http://127.0.0.1:8080/api`. Driver dùng eKYC `https://localhost:8443`. goAdmin gọi backend đặt xe và eKYC local từ trình duyệt. Biến môi trường của frontend được đặt cho phiên chạy. CA local được thêm vào truststore riêng cho JVM test. eKYC dùng thư mục hồ sơ riêng cho Phase 11.7 và cùng project Firebase test được người dùng cho phép. Không thay đổi hồ sơ production hoặc tài khoản Firebase.

## Thao tác trên thiết bị

1. Đăng nhập Driver bằng tài khoản test. Vào hồ sơ xác minh, thử quyền camera và chụp giấy tờ tổng hợp hoặc chọn ảnh trong album `Phase11-7`. Album chứa hai mặt CCCD, hai mặt GPLX và ảnh placeholder cho selfie. Placeholder kiểm tra upload, không dùng để đo chất lượng nhận diện mặt.
2. Bấm **Đọc thông tin tự động**, đối chiếu đề xuất với ảnh. Nếu thiếu, điền thủ công theo mẫu. Chọn **Không thời hạn** vì trạng thái hợp lệ đã biết của ca test, xác nhận cảnh báo và nộp. Có thể nhập hạng `A1, B` để kiểm tra nhiều hạng.
3. Tại goAdmin, đăng nhập admin test có quyền xét duyệt. Mở hồ sơ, xem ảnh và cảnh báo, duyệt nhóm xe phù hợp. Có thể chạy thêm một lượt yêu cầu bổ sung và nộp lại trước khi duyệt.
4. Trên Driver, kiểm tra trạng thái đã đồng bộ, bật nhóm xe đã được phép, bật online. Trên Pixel_4a, đăng nhập hoặc đăng ký Customer test, thêm xe phù hợp, chọn xe và đặt chuyến lái hộ. Dùng pickup trong vùng phục vụ, gần vị trí Driver.
5. Driver nhận và chấp nhận offer. Kiểm tra Customer nhận trạng thái mới, rồi hoàn thành chuyến theo luồng hai ứng dụng. Ghi nhận mã lỗi hoặc trạng thái theo từng bước. Dùng dữ liệu synthetic cho ảnh, field và chuyến.

## Phạm vi bằng chứng

Backend ghi nhận Driver đã mở kết nối WebSocket có xác thực. Customer gặp `ACCOUNT_NOT_FOUND` và DB test chưa có Customer. Đăng nhập admin, camera thật, gửi hồ sơ, quyết định goAdmin, đồng bộ quyết định, matching và hoàn thành chuyến còn chờ kiểm thử. Endpoint nội bộ trả 200 từ JVM dùng CA local. Chưa có quyết định mới để kiểm tra projection. Face comparison duy trì OFF.

- Ảnh CCCD tổng hợp trả HTTP 200 với bốn đề xuất QR. Worker OCR chạm deadline 55 giây và bị thu hồi. Lượt GPLX chưa có kết quả xác nhận.
- Expo qua lint, TypeScript, kiểm tra kiến trúc và 176 test. Ruff đạt. Pytest có 464 ca đạt, hai ca worker hết deadline trước khi tiến trình con sẵn sàng trong lượt chạy song song. Chưa xác nhận kết quả khi chạy riêng.
- Backend đặt xe build thành công và lượt test mặc định báo 154 đạt, 43 skip. Lượt PostgreSQL riêng và lượt goAdmin toàn bộ chưa thu được kết quả cuối.
- Phiên chạy bị ngắt. Kiểm tra sau đó cho thấy các cổng dịch vụ của Phase 11.7 đã đóng. Kết luận hiện tại là `E2E_PENDING`.
