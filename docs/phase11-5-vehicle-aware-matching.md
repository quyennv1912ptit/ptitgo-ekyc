# Phase 11.5 — Ghép chuyến theo quyền lái và phương tiện

## Thiết kế

Mở rộng `DriverVerificationEligibility` hiện có qua `mayDriveForRide`. Khi đồng bộ eKYC được bật, backend kiểm tra hồ sơ đã duyệt và feed hiện hành, không có ràng buộc kích hoạt đã chặn grant, đúng revision, GPLX và CCCD còn hiệu lực, capability phù hợp, preference đang bật, cùng trạng thái tài xế phù hợp với từng giai đoạn. Bộ chọn tài xế giữ logic bán kính, thứ tự ưu tiên, tín dụng và điều kiện hiện có. Chế độ đồng bộ tắt tiếp tục dùng quy tắc legacy của hệ thống.

- Snapshot Phase 11.4 gắn nhóm cụ thể theo thông số khách khai được backend ánh xạ, phiên bản catalog, loại xe và hộp số. Nhóm đó phải đang có hiệu lực trong hồ sơ tài xế. Danh mục ghép xe máy theo nhóm dung tích/công suất hiện có. Hai mẫu xe cùng nhóm catalog và cùng phạm vi GPLX không có preference riêng ở mức hãng/mẫu; không dùng tên mẫu để suy ra cấu hình.
- Với snapshot hợp lệ nhưng thiếu thông số hoặc chuyến cũ không có snapshot, backend yêu cầu đủ quyền cho mọi nhóm khả dĩ theo loại xe và hộp số. Nhóm có ID nhưng sai loại xe, hộp số, trạng thái hoặc phiên bản catalog bị loại. Không mở rộng bán kính ngoài cấu hình hoặc dùng tài xế không đủ hạng.
- Có thể bật online khi ít nhất một nhóm đang bật còn hiệu lực. Ví dụ tài xế A1 có thể online với nhóm phù hợp và nhận một chuyến 125 cc có snapshot phù hợp, nhưng chuyến xe máy thiếu thông số vẫn cần bao phủ toàn bộ nhóm khả dĩ.
- Snapshot hiện đánh dấu thông số là khách tự khai. Điều kiện đúng loại xe thực tế vẫn phụ thuộc hồ sơ xe và bước đối chiếu khi bàn giao. Ghép chuyến không xác minh độc lập giấy đăng ký xe hay điều kiện kinh doanh của dịch vụ.

## Các điểm kiểm tra

1. Candidate selection áp dụng snapshot của chuyến khi quét bán kính.
2. Offer creation khóa bản ghi chuyến trước bản ghi tài xế, rồi kiểm tra lại quyền, trạng thái và vị trí trước khi ghi offer bền vững. Thứ tự khóa cùng chiều với gán/nhận chuyến. Reservation Redis được nhả nếu không ghi được offer.
3. Khi nhận offer, token, tài xế và generation phải khớp sổ offer PostgreSQL. Câu UPDATE kiểm tra generation, expiry và ride state. Trong cùng transaction backend kiểm tra lại quyền dưới khóa tài xế. Khi quyền mất trước lúc nhận, từ chối, giải phóng reservation và tiếp tục lượt tìm nếu chuyến vẫn đang tìm. Chỉ replay thành công bằng token của offer đã được chấp nhận ở generation hiện tại.
4. Chuyến đặt lịch dùng ngày Việt Nam của thời điểm muộn hơn giữa hiện tại và thời điểm đón dự kiến. Kiểm tra lại khi nhận và trước khi bắt đầu. Đến ngày hôm sau khi GPLX vừa hết hạn sẽ bị chặn. Nếu đã bắt đầu chuyến, thay đổi preference không chuyển chuyến khỏi trạng thái đang thực hiện.
5. goAdmin giữ luồng tìm theo vị trí hoặc tìm tên. Mỗi trang ứng viên được lọc bằng `POST /api/admin/rides/{id}/assignable-drivers`. Submit vẫn kiểm tra lại cùng điều kiện trong backend và dùng reservation Redis hiện có.
6. Trước khi bắt đầu lái, backend kiểm tra lại phạm vi của tài xế đang thực hiện chuyến. Khi thiếu bằng chứng mới hoặc quyền bị thu hồi, yêu cầu bắt đầu bị từ chối.

Luồng WebSocket/polling, offerId và các trạng thái chuyến giữ nguyên contract. Khi tìm hết thời gian, Customer báo chưa có tài xế nhận lái loại phương tiện này.

## Kiểm thử và bàn giao

Dữ liệu tổng hợp kiểm tra ô tô số tự động/số sàn, nhóm xe máy không phù hợp, nhiều hạng GPLX, hết hạn/không thời hạn, preference tắt, hồ sơ chờ duyệt/thu hồi, offer sai generation và chuyến đặt lịch qua nửa đêm Việt Nam. Màn gán goAdmin kiểm tra ứng viên bị lọc và conflict khi điều kiện thay đổi. Cần xác minh qua hai thiết bị Android và các dịch vụ staging trước khi bật luồng eKYC cho người dùng thật. Không có migration mới trong Phase 11.5. Dừng trước Phase 11.6.


### Kết quả đã xác minh

- Gradle test và bootJar đạt. Hai suite tích hợp cơ chế phân phối và chuyến đã đặt lịch đạt trên PostgreSQL tạm. Container test đã được dọn.
- Expo đạt TypeScript, lint, kiểm tra kiến trúc và 175 test.
- goAdmin đạt TypeScript, lint và hai ca UI gán tài xế theo vị trí hoặc tìm theo tên. Vitest chạy có chọn lọc hai ca liên quan.
- Ruff đạt trong workspace tài liệu eKYC.
