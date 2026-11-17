# Phase 11.3 — Quyền lái và lựa chọn nhận chuyến

## Luồng và dữ liệu

- `LicenseEligibilityPolicy` Phase 11.2 tính phạm vi GPLX. goAdmin chỉ hiện lựa chọn duyệt cho nhóm có kết quả `ELIGIBLE`. Đổi ngày cấp, hạn chế hoặc bản nộp làm mất lựa chọn cũ. Người duyệt chọn một tập con đang phù hợp.
- Quyết định eKYC gắn bằng chứng admin đối chiếu và ID của các nhóm đã chọn với revision đã nộp. Luồng nội bộ có xác thực chuyển dữ liệu này cùng entitlement từ snapshot bất biến. Phê duyệt theo API cũ vẫn hợp lệ cho hồ sơ xác minh và sinh danh sách quyền nhận chuyến rỗng nếu chưa có bước duyệt nhóm.
- Backend đặt xe tính lại mỗi nhóm theo policy, danh mục và các entitlement của snapshot. Yêu cầu vượt phạm vi, nhóm ngoài MVP và phiên bản policy khác không tạo grant. Grant lưu ID hồ sơ, revision bản nộp, revision duyệt, phiên bản policy và các entitlement đủ điều kiện kèm hạn.
- Projection Phase 11.0 tái sử dụng để lưu grant theo nhóm. Bảng `driver_capability_preferences` của PostgreSQL lưu lựa chọn theo revision duyệt và phiên bản chỉnh sửa. Cả đồng bộ và đổi preference khóa cùng bản ghi tài xế. Một cập nhật đồng thời trên cùng phiên bản thành công, cập nhật còn lại nhận 409. Bản duyệt mới mặc định tắt toàn bộ nhóm.
- Revocation, yêu cầu bổ sung, bản duyệt cũ, dữ liệu đồng bộ chưa bắt kịp, CCCD/GPLX hết hạn hoặc phiên bản luật không còn khớp đều có thể làm mất quyền hiệu lực. Khi tài xế tắt nhóm cuối cùng có thể nhận cho loại xe đang online, backend chuyển phiên nhận chuyến về offline. Lượt chạy matching và bước nhận chuyến đều kiểm lại quyền, kể cả khi trạng thái hiện diện cũ còn tồn tại. Chuyến đang thực hiện được giữ.

## API và giao diện

| Vai trò | Điểm truy cập |
| --- | --- |
| Driver | `GET /api/driver/capabilities` và `PUT /api/driver/capabilities/{groupId}/preference` trên backend đặt xe. Identity từ phiên đăng nhập. PUT nhận `approvalRevision`, `preferenceVersion` và `enabled`. |
| goAdmin | Preview dùng API Phase 11.2. Approve gửi `capabilityReview` qua API eKYC hiện có. `GET /api/admin/vehicle-catalog/applications/{applicationId}/capabilities` đọc preference của bản hiện hành. |
| Expo Driver | **Hồ sơ → Loại xe nhận lái hộ** hiển thị nhóm đã duyệt, nhóm đã bật, hạn hoặc lý do không thể bật. Trang chủ lấy các loại xe có thể online từ backend trong chế độ eKYC. |

### Giới hạn hiện tại

- Chuyến hiện tại dùng mã loại xe và hộp số, dữ liệu có thể thiếu chi tiết nhóm hoặc thông số xe. Matching với dữ liệu thiếu chi tiết chỉ nhận tài xế nếu các nhóm đã bật bao phủ toàn bộ khả năng trong phạm vi MVP. Ô tô có hộp số rõ có thể nhận đúng hộp số được duyệt, ô tô thiếu hộp số cần bao phủ cả hai. Với chuyến xe máy chỉ có mã `MOTORCYCLE`, quyền A1 hạn chế chưa đủ để nhận mọi nhóm mô tô. Chưa thay đổi schema xe khách hàng trong phase này.
- Snapshot eKYC hiện ghi một cấu trúc hạn chung. Nếu nhiều hạng trên cùng GPLX có hạn hoặc hạn chế theo từng hạng mà snapshot không phân biệt được, policy chỉ cấp phạm vi có bằng chứng đầy đủ. Hạn chế chưa rõ và hạn chế không quy được cho một hạng cần đối chiếu.
- Chế độ nhận chuyến eKYC dùng cờ `DATXE_VERIFICATION_SYNC_ENABLED` hiện có. Khi bật, tài xế cũ thiếu grant cần tái xác minh trước khi online hoặc nhận chuyến bằng chế độ này. Chế độ legacy dùng cài đặt cũ khi tính năng đồng bộ chưa bật. Tắt cờ quay về nghiệp vụ legacy, vì vậy không dùng cách đó để thay cho thu hồi quyền khi đang vận hành dịch vụ lái hộ.
- Migration V30 là bổ sung. Cập nhật backend và app cùng đợt staging. Dữ liệu đã duyệt và preference giữ nguyên khi rollback phiên bản, nhưng phải ngừng luồng phục vụ hoặc điều hướng sang mode phù hợp trước khi rollback thực thi policy.

## Kiểm thử

Dùng dữ liệu tổng hợp để kiểm tra nhiều hạng, từ chối nhóm ngoài phạm vi, tùy chọn mặc định tắt, bật/tắt, hết hạn, thu hồi và bản duyệt mới. PostgreSQL test tạm chạy V30 và hai cập nhật đồng thời với cùng phiên bản preference. Android thật và luồng login xuyên cả ba dịch vụ vẫn cần xác minh trên staging. Không đổi model, ngưỡng hoặc môi trường đang chạy.


### Kết quả chạy

- eKYC đạt Ruff và 463 test Pytest.
- Backend đặt xe đạt Gradle test và bootJar. Ca tích hợp V30 trên PostgreSQL tạm cùng kiểm thử cạnh tranh cập nhật đạt. Container test đã được dọn.
- Expo đạt kiểm tra kiến trúc, TypeScript, lint và 174 test, sau khi sinh route types cho trang mới.
- goAdmin đạt TypeScript, lint và 7 test màn hình duyệt. Test xác nhận chỉ các nhóm đủ điều kiện được chọn để gửi quyết định.
