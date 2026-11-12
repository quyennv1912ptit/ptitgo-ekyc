# Dùng dataset Vietnamese ID Card cho bộ phát hiện trường

Ngày kiểm tra: 10/11/2026.
Nguồn: https://universe.roboflow.com/a-asa/vietnamese-id-card-uuil0-7krw7

Trang công khai hiện ghi 1.740 ảnh, task Object Detection, 8 lớp và giấy phép CC BY 4.0.
Đồng thời trang ghi Dataset versions = 0 và Models = 0. Phần API/demo ghi
“Powered by general detection model”; chưa xác nhận có checkpoint CCCD pretrained tải về.
Ảnh có box không chứng minh mô hình đã đọc đúng chuỗi ký tự.

## Phần dùng được trong service

Dataset này phù hợp làm nguồn train **field detector**: định vị vùng thông tin trên ảnh thẻ.
Không thay trực tiếp checkpoint pose 4 góc hiện tại: nhãn và nhiệm vụ khác nhau.

| Lớp dataset | Trường API hiện tại |
|---|---|
| id | idNumber |
| name | fullName |
| dob | dob |
| gender | gender |
| origin_place | placeOfOrigin |
| current_place | address |
| expire_date | expiryDate |
| nationality | Chưa có trong response; có thể giữ nội bộ nếu cần |

Luồng ứng dụng dự kiến:

```text
Ảnh chụp → CardPreparer (4 góc + warp) → FieldDetector (vùng từng trường)
         → OCR từng vùng → chuẩn hóa/kiểm tra → response hiện tại
```

Đây là đề xuất kiến trúc, chưa tích hợp hoặc đo hiệu năng trên dataset này.
Lợi ích cần kiểm chứng: xác định trường theo class detector thay vì bắt buộc OCR đọc đúng
nhãn “Ngày sinh”, “Có giá trị đến”, nhờ đó giảm lỗi ngày đã đọc đúng nhưng không gán được.
Không bảo đảm sửa lỗi dấu/ngày bị OCR đọc sai. Class current_place có thể có nhiều box;
cần ghép theo dòng và giữ đủ nội dung. Không coi thiếu box là trường không tồn tại.

Nên crop ROI từ ảnh nắn có độ phân giải cao còn giữ được, với biên vừa đủ để không cắt nét chữ.
Hiện CardPreparer trả ảnh 1600×1009; muốn tận dụng native crop cần thiết kế lại dữ liệu nội bộ
hoặc vị trí tích hợp, không chỉ thay tên model trong .env. Giữ normalizer, kiểm tra và review hiện có.

## Ảnh bị kéo giãn

Với các ảnh đã crop/kéo giãn như mẫu, không trộn trực tiếp vào bộ pose 4 góc ảnh chụp thật:
không có nhãn góc thẻ cùng loại và bối cảnh nền khác. Không suy ra 4 góc thẻ chụp thật từ
4 góc ảnh đã crop hoặc từ các box chữ.

Ảnh kéo giãn vẫn có thể dùng train detector vùng trường nếu box chính xác. Cần xem ảnh gốc,
preprocessing và nhãn trước khi quyết định chuẩn hóa tỷ lệ. Mọi biến đổi ảnh phải biến đổi
tọa độ box tương ứng; không tự coi mọi ảnh trong dataset là một thẻ đã nắn đúng.
Đánh giá trên ảnh nắn từ JSON của project rồi mới ghép với YOLO góc, vì domain có thể khác.

## Lấy dữ liệu và kiểm chứng tiếp

1. Nếu tài khoản cho phép, Fork dataset → Generate version → Export YOLOv8 Object Detection.
   Trang hiện chưa có version public để chọn tải trực tiếp. Chưa tải ảnh hay dùng API demo
   với bất kỳ ảnh CCCD riêng tư nào của người dùng.
2. Kiểm tra archive, tên lớp, số ảnh, split, nguồn/gộp ảnh trùng và nhãn. Giữ attribution nguồn;
   không suy từ giấy phép dataset ra quyền tải hoặc giấy phép trọng số chưa được công bố.
3. Train model **detect**, không dùng nguyên notebook pose `kpt_shape=[4,3]` đang có.
4. So sánh trên cùng ảnh: JSON warp + OCR/parser hiện tại; JSON warp + field detector + OCR;
   sau đó YOLO warp + field detector + OCR. Tách lỗi vùng thẻ, vùng trường, đọc ký tự và gán trường.
5. Đánh giá missing field, sai ký tự, địa chỉ nhiều dòng, vùng bị che và false detections.
   Các ảnh cùng thẻ phải cùng split; cần tập kiểm tra độc lập để kết luận tổng quát hóa.

## Nguồn chính thức

- Dataset: https://universe.roboflow.com/a-asa/vietnamese-id-card-uuil0-7krw7
- Detection output gồm box, class, confidence: https://docs.ultralytics.com/tasks/detect/
- Fork/generate/export dataset: https://roboflow.com/blog/find-computer-vision-datasets-with-chatgpt
- License được trang dataset liên kết: https://creativecommons.org/licenses/by/4.0/
