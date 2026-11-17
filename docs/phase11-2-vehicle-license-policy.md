# Phase 11.2 — Danh mục xe và chính sách GPLX

## Danh mục dùng chung

Backend đặt xe phát `GET /api/vehicle-catalog` qua xác thực hiện có. Phản hồi có phiên bản `VN_MVP_1`, mã loại xe dùng cho đặt chuyến, mã hộp số và danh sách nhóm phương tiện có cờ `active` / `supported`. Catalog lưu nhóm, phân nhóm, truyền động, kiểu động cơ, dung tích, công suất, số chỗ hành khách không tính tài xế, khối lượng toàn bộ theo thiết kế, khối lượng rơ-moóc và các trường hãng, mẫu, phiên bản.

Seed là các nhóm đại diện và điểm biên của mỗi nhóm, gồm mô tô xăng, mô tô điện và ô tô cá nhân số tự động/số sàn. Hãng, mẫu và phiên bản để trống vì seed mô tả nhóm. Xe khách, xe tải và ô tô kéo rơ-moóc có mục mở rộng nằm ngoài dịch vụ MVP.

Customer và Driver dùng chung client catalog trong `@datxe/shared`. Màn hình đặt xe và khai kỹ năng lấy loại xe từ catalog, màn hình quản lý giá lấy cùng danh sách. Pricing và matching kiểm tra loại xe đang được hỗ trợ trên backend. Khi API catalog chưa có, các giao diện dùng danh sách tương thích hiện có trong lúc cập nhật phiên bản.

Audit thấy `VehicleType`, `DriverSkill`, bản ghi xe khách hàng, giá và chuyến hiện sử dụng `CAR` / `MOTORCYCLE`. SQL đã có CHECK constraint cho hai giá trị này và cho `AUTO` / `MANUAL`. Seed có phiên bản trong code và API là bổ sung, vì vậy không cần migration hay backfill. Các bản ghi giá, xe khách hàng, kỹ năng và quyền nhận chuyến hiện hữu được giữ nguyên. Matching tiếp tục kiểm tra entitlement và kỹ năng đã duyệt như Phase 11.0–11.1.

## Máy luật và nguồn

**Policy version** `VN_36_2024_108_2026_MVP_1`. Kết quả của mỗi nhóm gồm `ELIGIBLE`, `INELIGIBLE` hoặc `REQUIRES_MANUAL_REVIEW`, kèm `reasonCodes` và `policyVersion`. Ngày đánh giá lấy theo `Asia/Ho_Chi_Minh`. Các hồ sơ thiếu ngày cấp, chưa xác minh được hạn chế, có hạng không phù hợp chế độ cấp hoặc có dữ liệu mâu thuẫn cần đối chiếu. Mã chưa biết hoặc hạn chế về xe cải tạo đưa sang duyệt thủ công.

| Rule | Cách áp dụng và hiệu lực |
| --- | --- |
| A1 / A / B1 theo chế độ mới | Từ 01/01/2025, phạm vi A1 đến 125 cm³ hoặc 11 kW. Hạng A bao gồm nhóm trên ngưỡng và nhóm A1. B1 mới có phạm vi mô tô, trong MVP xét nhóm hai bánh thuộc A1. [Điều 57, VBHN 55/VBHN-VPQH](https://datafiles.chinhphu.vn/cpp/files/vbpq/2026/3/55-vbhn-vpqh.pdf) |
| A1 / A2 chưa đổi, cấp lại | GPLX cấp trước 01/01/2025 tiếp tục áp dụng hạn in trên hồ sơ. Với nhóm mô tô MVP, A1 cũ ở khoảng 50 đến dưới 175 cm³ hoặc 4 đến dưới 14 kW. A2 bao gồm các nhóm mô tô hai bánh có thông số hỗ trợ. [Điều 89 khoản 1–2](https://datafiles.chinhphu.vn/cpp/files/vbpq/2026/3/55-vbhn-vpqh.pdf) |
| B1 cũ và lái có thu phí | Từ 01/01/2025, thực hiện theo điều khoản chuyển tiếp. Hai loại B1 cũ được cấp cho người không hành nghề lái xe. Preview lái hộ có thu phí trả `LEGACY_B1_NON_PROFESSIONAL`. Nếu đánh giá phạm vi lái riêng, vẫn phải xác minh hạn chế hộp số. [Điều 89 khoản 2 điểm đ–g](https://datafiles.chinhphu.vn/cpp/files/vbpq/2026/3/55-vbhn-vpqh.pdf) |
| Ô tô cá nhân và thông số xe tải | Từ 01/01/2025, phạm vi hạng B gồm ô tô đến 8 chỗ hành khách, và giới hạn thiết kế xe tải/xe chuyên dùng đến 3.500 kg. C1 có bậc đến 7.500 kg. Luật phân ranh rơ-moóc ở 750 kg. Xe tải chỉ dùng các ngưỡng này trong kiểm thử rule mở rộng. Xe khách, xe tải và rơ-moóc vẫn ngoài MVP. [Điều 57 khoản 1](https://datafiles.chinhphu.vn/cpp/files/vbpq/2026/3/55-vbhn-vpqh.pdf) |
| Hạn chế hộp số và hạng A chuyển đổi | Từ 01/07/2026, Phụ lục IX Thông tư 108/2026/TT-BCA ghi `B.01` cho ô tô hạng B số tự động và `A.03` cho mô tô hai bánh dưới 175 cm³ hoặc dưới 14 kW. Phạm vi chuyển đổi A1 cũ cũng nằm tại Điều 89 khoản 3 điểm a. Các mã này cũng xuất hiện trong mẫu trước tháng 7/2026. Hệ thống chỉ áp dụng mã khi khớp hạng đã xác định. [Phụ lục mẫu trước tháng 7/2026](https://congbao.chinhphu.vn/tai-ve-van-ban-so-12-2025-tt-bca-44366-55284?format=pdf) [Thông tư 108/2026/TT-BCA](https://vanban.chinhphu.vn/?classid=1&docid=218725&pageid=27160&typegroupid=6) · [Điều 89 khoản 3](https://datafiles.chinhphu.vn/cpp/files/vbpq/2026/3/55-vbhn-vpqh.pdf) |
| Thời hạn | Luật mới áp dụng từ 01/01/2025. Các hạng A1/A/B1 mới không thời hạn. Các hạng ô tô áp dụng thời hạn theo hạng. Preview dựa trên ngày hết hạn ghi nhận và không suy ra trạng thái không thời hạn khi thiếu dữ liệu. [Điều 57 khoản 5 và 7, Điều 89 khoản 1](https://datafiles.chinhphu.vn/cpp/files/vbpq/2026/3/55-vbhn-vpqh.pdf) |

Preview đánh giá phạm vi GPLX, chưa xác minh độc lập hồ sơ điểm, tước quyền sử dụng, tình trạng thu hồi, sức khỏe hoặc điều kiện kinh doanh của mô hình lái hộ có thu phí. Điều kiện kinh doanh cần rà soát riêng trước pilot vì phụ thuộc mô hình cung ứng và vận hành thực tế. [Luật Đường bộ 35/2024/QH15](https://vanban.chinhphu.vn/?classid=1&docid=211193&orggroupid=1&pageid=27160)

## Xem trước và vận hành

Trong goAdmin, mở bản nộp đang xét duyệt, đối chiếu GPLX, nhập ngày cấp và chọn hạn chế đã xác minh, rồi bấm **Xem nhóm xe**. Hạng và hạn lấy từ bản nộp. Preview gọi `POST /api/admin/vehicle-catalog/license-preview` bằng quyền Admin, chỉ gửi thông tin phục vụ đánh giá và không cập nhật quyết định hoặc quyền nhận chuyến. Việc lưu năng lực đã duyệt và preference của tài xế thuộc Phase 11.3.

Kiểm thử bằng dữ liệu tổng hợp. Chủ đề gồm HTTP catalog, ranh giới dung tích/công suất/số chỗ/khối lượng, chế độ cấp cũ/mới, lái có thu phí, mã hạn chế, hết hạn và xem trước khi duyệt. Kiểm tra thủ công trên staging gồm mở danh mục ở Customer và Driver, đối chiếu hai lựa chọn với pricing và thử preview GPLX tổng hợp tại goAdmin. Kiểm tra Android thật vẫn chưa thực hiện.


### Kết quả kiểm thử

- Backend đặt xe đạt Gradle test và bootJar. 144 test đạt, 43 test theo điều kiện môi trường được bỏ qua. Sáu test mới xác minh máy luật, HTTP catalog và kết quả xem trước.
- Expo Customer/Driver đạt kiểm tra kiến trúc, TypeScript, lint và 173 test.
- goAdmin đạt TypeScript, lint và 6 test màn hình xét duyệt, gồm ca preview không gửi trường định danh và không thay đổi quyết định.
- Ruff đạt trong workspace eKYC.
