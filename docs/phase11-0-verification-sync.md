# Phase 11.0 — Đồng bộ xác minh tài xế

## Quyền quyết định và dữ liệu

eKYC giữ hồ sơ, ảnh, quyết định admin và revision gốc. Backend đặt xe giữ quyền online và điều phối chuyến, dựa trên bản sao tối thiểu của kết quả duyệt khi bật đồng bộ. Firebase UID liên kết hai hệ thống. Bản sao chỉ nhận mã hồ sơ, UID, revision, trạng thái và hạng GPLX kèm thông tin hạn. Ảnh, số giấy tờ, tên, ngày sinh và lý do admin nằm trong workflow eKYC.

eKYC ghi sự kiện cùng giao dịch SQLite tạo quyết định hoặc chuyển trạng thái hồ sơ. Hồ sơ có từ trước được bổ sung từ trạng thái hiện tại khi mở database lần đầu. Admin có thể thu hồi hồ sơ đã duyệt và yêu cầu bổ sung có chỉ định ảnh/trường để tái xác minh. Khi cần duyệt lại, quyền nhận chuyến bị thu hồi trong thời gian xử lý.

Backend đặt xe dùng V28 để lưu bản sao, stream ID và checkpoint theo PostgreSQL. Worker lấy sự kiện theo thứ tự, ghi bản sao và checkpoint trong một giao dịch, tự thử lại từ checkpoint khi gặp lỗi. Revision cũ bị bỏ qua. Stream thay đổi, checkpoint bất nhất hoặc truy cập nguồn lỗi khiến hệ thống chặn cấp quyền. Rollback/restore nguồn phải kiểm tra lại stream và checkpoint trước khi mở quyền; không tự reset checkpoint theo client.

## Cách tính quyền

- Online, matching, giao dịch nhận offer và admin gán chuyến kiểm tra trạng thái đồng bộ hiện tại. Các điều kiện ví, khả dụng, loại xe, kỹ năng và hạn chế truyền động hiện có tiếp tục áp dụng.
- Bản sao chỉ được dùng trong 10 giây kể từ khi worker đã bắt kịp. Khi xét quyền, backend đối chiếu stream ID và head với nguồn eKYC. Matching đối chiếu một lần cho mỗi batch. Giao dịch nhận chuyến và admin gán kiểm tra lại, dưới lock tài xế dùng chung với cập nhật đồng bộ.
- Hạn được kiểm tra theo Asia/Ho_Chi_Minh. `NO_EXPIRY` phải được nêu rõ trong quyết định, `HAS_EXPIRY` phải có ngày hợp lệ, còn `UNKNOWN` hoặc `EXPIRED` bị chặn. Mỗi hạng được kiểm tra riêng và phải khớp kỹ năng đã duyệt. Form eKYC hiện có một ngày hạn cho GPLX, nên các hạng trên cùng giấy phép nhận cùng khai báo này. Chưa tạo danh mục pháp lý theo hạng hoặc subtype xe.
- Tình huống quyết định ở eKYC thay đổi ngay sau lần kiểm tra head vẫn có cửa sổ xuyên dịch vụ. Đồng bộ giao dịch khóa thứ tự cập nhật ở backend đặt xe; các bước sau phải kiểm tra lại. Phase này không hủy chuyến đang chạy.

## Cấu hình và tương thích

Mặc định giữ workflow local hiện có. Khi muốn kiểm thử đồng bộ:

- eKYC đặt `VERIFICATION_SYNC_KEY` ngẫu nhiên dài tối thiểu 32 ký tự và chạy API workflow xác minh có database/Firebase đã cấu hình.
- Backend đặt xe đặt `DATXE_VERIFICATION_SYNC_ENABLED=true`, cùng khóa trong `DATXE_VERIFICATION_SYNC_KEY`, và `DATXE_VERIFICATION_SYNC_ORIGIN` là origin HTTPS của workflow eKYC. HTTP chỉ được cấu hình cho loopback dev. Với CA local, cấp trust trong JDK của backend test.
- Hai endpoint nội bộ `decision-events` và `decision-head` xác minh khóa riêng. Driver/admin token không có quyền truy cập feed. Giữ khóa ở server, không đưa vào Expo hoặc goAdmin.
- Khi bật đồng bộ, Driver đọc trạng thái qua API đặt xe và goAdmin đọc trạng thái qua projection. Quyết định duyệt thực hiện trên hồ sơ eKYC. Tài xế chưa có hồ sơ đã đồng bộ sẽ không được bật online.
- Workflow Driver tiếp tục trích xuất qua endpoint eKYC có kiểm tra ownership và revision. Khi bật đồng bộ, route nhận ảnh thô `/api/driver/ekyc` ở Spring được tắt để chỉ còn một đường thực hiện OCR cho workflow xác minh. Deployment Modal chỉ inference chưa phải origin đồng bộ hồ sơ.

Chỉ bật trên môi trường test hoặc rollout đã đối soát hồ sơ cũ. Giữ cờ OFF khi cần giữ nguyên chế độ legacy. Dùng dữ liệu tổng hợp trong kiểm thử.

## Kiểm thử local

- eKYC Ruff và 459 Pytest đạt. Integration API kiểm tra khóa nội bộ, approve/reject, revoke/reverify, revision cũ và dữ liệu tối thiểu của feed.
- Backend đặt xe Gradle và bootJar đạt. PostgreSQL 17 tạm kiểm tra migration V28, checkpoint, replay, revision cũ, stream sai, retry và trạng thái goAdmin. Chạy riêng 5 ca liên quan trên bản cuối, gồm khởi động Spring với sync bật và kiểm thử HTTP transport giả lập. Tất cả đạt.
- Expo architecture, TypeScript, lint và 172 test đạt. goAdmin TypeScript, lint và 4 test review đạt.
- Chưa chạy luồng hai điện thoại cùng backend đặt xe và eKYC thật trong môi trường staging. Cờ đồng bộ chưa bật trên dịch vụ đang chạy.
