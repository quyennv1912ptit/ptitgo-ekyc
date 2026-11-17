# Phase 11.4 — Garage khách hàng

## Luồng sử dụng

Trong Customer, mở **Hồ sơ → Phương tiện của tôi**. Có thể thêm hoặc sửa xe, chọn mẫu từ catalog, nhập hãng/dòng xe riêng, đặt mặc định bằng biểu tượng ngôi sao hoặc lưu trữ xe khỏi danh sách đặt chuyến. Chọn mẫu chỉ điền hãng và dòng xe. Dung tích, công suất, số chỗ hành khách (không kể tài xế), khối lượng theo thiết kế và rơ-moóc phải khai theo giấy tờ xe. Garage hiện ghi nguồn `CUSTOMER_DECLARED`.

Chọn hoặc giữ xe mặc định tại bước chọn phương tiện trước khi báo giá. Nếu xe đã được chọn tường minh sau đó bị lưu trữ, app cần khách chọn lại thay vì tự thay xe. Màn xác nhận hiện có vẫn gửi `vehicleId`. Giá lấy theo xe được backend xác thực. Logic đặt ngay, đặt lịch, voucher, thanh toán và idempotency dùng các đường xử lý hiện có.

## API và dữ liệu

- Giữ API CRUD và ảnh tại `/api/customer/vehicles`. Thêm `PUT /api/customer/vehicles/{id}/default`. GET chỉ trả xe đang hoạt động và xếp xe mặc định trước. DELETE chuyển xe sang trạng thái lưu trữ, bỏ mặc định và giữ dữ liệu liên kết với chuyến cũ. Lệnh gọi lại trên chính xe đã lưu trữ vẫn thành công. Biển số của xe đã lưu trữ có thể dùng để đăng ký xe mới.
- Backend kiểm tra quyền sở hữu từ phiên đăng nhập khi đọc/sửa/lưu trữ/đặt mặc định. PostgreSQL bảo đảm tối đa một xe mặc định mỗi khách. Cập nhật thông tin, lưu trữ và tạo chuyến khóa cùng bản ghi khách hàng để tránh chụp thông số giữa hai lần cập nhật.
- `GET /api/vehicle-catalog` thêm danh sách hãng/dòng xe để chọn. Phạm vi nhóm vẫn lấy từ catalog Phase 11.2. Backend ánh xạ hộp số, số chỗ, rơ-moóc, dung tích hoặc công suất đã khai sang nhóm. Nếu chưa đủ thông số, snapshot lưu `NEEDS_DETAILS` và không sinh ID nhóm. Thông số cho thấy xe ngoài nhóm MVP sẽ bị từ chối ở bước báo giá/tạo chuyến.
- V31 thêm thông số garage, trạng thái mặc định/lưu trữ và `rides.vehicle_booking_snapshot`. Chuyến mới giữ hãng, mẫu, các thông số, nguồn, phiên bản catalog và kết quả ánh xạ nhóm tại lúc tạo. Loại xe, biển số, hộp số và ID xe vẫn dùng các cột snapshot đã có ở chuyến. Trigger bảo vệ phần snapshot mới khỏi ghi đè. API chi tiết chuyến trả thêm `vehicleSnapshot`; chuyến cũ có thể thiếu trường này. Không lấy dữ liệu garage hiện tại để tái dựng thông số lịch sử.
- Giữ ràng buộc loại xe của bản ghi đã từng có chuyến. Nếu cần sửa từ ô tô sang xe máy hoặc ngược lại, lưu trữ mục cũ rồi thêm lại. Các thông số khác có thể cập nhật.
- Hiện chưa có quy trình xác minh thông số xe. Giai đoạn này cung cấp snapshot và yêu cầu theo nhóm cho bước tiếp theo. Matching vẫn áp dụng chính sách Phase 11.3.

## Kiểm thử và chuyển phase

Các test backend dùng phương tiện tổng hợp để kiểm tra ranh giới nhóm, quyền sở hữu, mặc định, lưu trữ, sửa xe sau khi đặt chuyến, snapshot bất biến, đặt ngay/đặt lịch và trả lại đúng chuyến khi gửi lại idempotency key. Test Customer kiểm tra xe mặc định và cách xử lý khi lựa chọn tường minh biến mất.

Trước staging, chạy Flyway V31 trên database test, mở garage Customer, tạo xe số tự động và mô tô điện, đặt mặc định, thử báo giá, sửa xe sau khi tạo chuyến và đối chiếu chi tiết lịch sử. Luồng thiết bị thật và dữ liệu staging vẫn cần xác minh. Dừng ở Phase 11.4.


### Kết quả đã xác minh

- Expo đạt kiểm tra kiến trúc, TypeScript, lint và 175 test, gồm lựa chọn xe mặc định và ca xe đang chọn bị lưu trữ.
- Ruff đạt trong workspace eKYC. Không có thay đổi runtime eKYC trong phase này.
- PostgreSQL 17 test tạm chạy migration V31 và ca tích hợp đặt ngay/đặt lịch, đổi xe mặc định, từ chối truy cập chéo, chỉnh sửa/lưu trữ xe, snapshot và replay idempotency. Ca tích hợp thực thi qua RideService/JPA với routing và Redis giả lập. Hai test ranh giới nhóm xe cũng đạt trong lượt này.
- Backend đạt lượt Gradle test và bootJar sau khi xác minh đọc lại snapshot từ PostgreSQL. Container test đã được dọn.
