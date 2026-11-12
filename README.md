# eKYC service — nền tảng API

Python 3.11, FastAPI, pydantic-settings. Có `GET /health` công khai và `POST /ekyc/cccd` yêu cầu API key. Lifespan tạo settings, semaphore, classifier, card preparer và EasyOCR (mặc định); face engine chưa triển khai.

## Chạy

```powershell
uv sync --frozen
# Chỉ khi chưa có .env:
Copy-Item .env.example .env
# Thay EKYC_API_KEY bằng secret thực tế.
uv run uvicorn ekyc.main:app --port 8001
curl.exe http://localhost:8001/health
```

`.env` hiện có được giữ nguyên. Mọi cấu hình có trong `.env.example`; biến cũ không còn dùng được bỏ qua. Settings mặc định được cache, nhưng `create_app(Settings(...))` hỗ trợ inject trong test. API key bắt buộc; `/docs`, `/redoc`, `/openapi.json` tắt khi `ENV=prod`.

## Hợp đồng response

Thành công: `{"success":true,"data":{},"warnings":[],"requestId":"..."}`.
Lỗi: `{"success":false,"errorCode":"IMAGE_BLURRY","message":"...","retryable":false,"warnings":[],"requestId":"..."}`.

Schema kế thừa `CamelModel`. Route sau này khai báo `response_model_exclude_none=True` để bỏ trường null. Lỗi BLOCK dùng HTTP 200; client phải kiểm tra `success`. Lỗi kỹ thuật dùng HTTP 400–504 theo `ERROR_META`. `/health` trả JSON thường.

Header `X-Request-ID` được lọc ký tự an toàn, giới hạn 64 ký tự; rỗng sau lọc thì sinh UUID mới. Response luôn trả header này, kể cả lỗi 500. Header `X-API-Key` được so sánh trên bytes bằng `compare_digest`. Các service sau này dùng `job_slot(app.state.job_slots, settings.job_slot_timeout_s)`.

Log một dòng có request_id. Stack trace giữ đủ khung file/hàm/dòng và loại exception; bỏ nội dung exception và dòng mã để không vô tình lộ URL có chữ ký hay thông tin cá nhân. Không log body, URL ảnh hoặc thông tin định danh chưa che. `mask_id_number` giữ ba ký tự đầu/cuối khi độ dài ít nhất bảy.

## Kiểm tra

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Test route chỉ được gắn trong fixture. Không cần mạng, model hay ảnh thật.

## Docker và bước tiếp theo

`docker compose up --build -d` chạy một worker và chỉ expose cổng 8001 trong mạng Docker. Dockerfile chưa tải model ở bước này. Dependency OCR/face và lockfile CPU được giữ từ setup ban đầu để dùng tiếp; EasyOCR chỉ được import khi backend easyocr được khởi tạo.

Bước sau: load/warm-up model trong lifespan, thêm Protocol engine, pipeline, services và endpoint CCCD/GPLX/face. Khi đó bổ sung lại script tải model và hiệu chỉnh ngưỡng cùng test tương ứng.

## Nhận diện loại và mặt giấy tờ từ OCR

`DocClassifier(settings).classify(lines, img_height)` là hàm xử lý không I/O, không OCR,
không đọc ảnh và không train model. Classifier được tạo một lần trong lifespan tại
`app.state.doc_classifier`. Đầu vào là `OcrLine` với text, confidence và tọa độ pixel
trên ảnh đã chạy OCR; `img_height` phải là chiều cao của chính ảnh đó.

```python
from ekyc.config import Settings
from ekyc.pipeline.doc_classifier import DocClassifier
from ekyc.pipeline.doc_types import DocType, Side
from ekyc.pipeline.ocr.base import OcrLine
from ekyc.services.doc_validation import ensure_document

settings = Settings()  # Cần EKYC_API_KEY trong .env.
classifier = DocClassifier(settings)
lines = [
    OcrLine("CĂN CƯỚC CÔNG DÂN", 0.95, 0, 80, 600, 120),
    OcrLine("Ho va ten Nguyen Van A", 0.9, 0, 580, 600, 620),
]
result = classifier.classify(lines, img_height=1000)
ensure_document(result, expected_type=DocType.CCCD, expected_side=Side.FRONT)
```

Bốn luật cố định: CCCD front/back, GPLX front và CMND front; từ khóa chỉ theo đặc tả.
Quốc hiệu không được tính điểm. CMND được nhận diện nhưng service từ chối với thông báo
chưa hỗ trợ. Chưa có luật GPLX back/CMND back nên không thể xác nhận các mặt đó.

Các ngưỡng `DOC_*` và danh sách `LICENSE_CLASS_CODES` nằm trong Settings và `.env.example`.
Điểm là tổng trọng số (chặn ở 1.0), không phải xác suất thống kê. Dòng confidence thấp bị bỏ;
thiếu chữ trả unreadable; điểm thấp hoặc hai loại sát điểm trả UNKNOWN; hai mặt sát điểm
chỉ giữ loại và trả side UNKNOWN. Khi hòa, chọn luật đứng trước để giữ kết quả xác định.
`runner_up` luôn so với loại khác, không lấy mặt còn lại của cùng loại.

Evidence chỉ gồm ID đặc trưng; vẫn giữ evidence của luật có điểm cao nhất khi kết quả UNKNOWN.
DEBUG chỉ ghi loại, mặt, điểm và evidence, không ghi OCR text. `ensure_document` quyết định
UNREADABLE_DOCUMENT/WRONG_DOCUMENT_TYPE/WRONG_SIDE theo đúng thứ tự kiểm tra.
Chiều cao ảnh không hợp lệ và confidence ngoài [0,1] báo ValueError cho caller sửa input.

Classifier hiện thay thế CNN; script train và test CNN đã bỏ. Không thêm dependency ngoài
rapidfuzz/unidecode (đều đã có). Các thư viện xử lý ảnh/model cũ được giữ cho bước khác;
package pipeline import các tiện ích ảnh khi sử dụng để classifier không kéo chúng vào startup.

Bước sau: triển khai OCR engine trả OcrLine, điều phối OCR/validation, xử lý xoay hoặc mặt
nếu cần; đối chiếu từ khóa/ngưỡng với thẻ thực tế và thêm endpoint. Luồng OCR và endpoint CCCD được triển khai ở bước 3 bên dưới; chưa đánh giá chất lượng trên ảnh thật.


## Bước 3: OCR và endpoint CCCD

```powershell
uv sync --frozen
uv run python scripts/download_models.py
$env:OCR_BACKEND="easyocr"
uv run uvicorn ekyc.main:app --port 8001 --workers 1 --no-access-log
```

Để chạy khung API không tải model: `$env:OCR_BACKEND="none"`. `/health` vẫn chạy;
`POST /ekyc/cccd` khi chưa có engine sẽ trả INTERNAL_ERROR. API key lấy từ `.env`.
EasyOCR dùng `vi,en`, CPU, Reader được chia sẻ với lock; load và warm-up một lần mỗi lifespan.
Model dùng cache mặc định của EasyOCR. Dockerfile giữ nguyên, chưa tải sẵn model khi build.

Body gửi `POST /ekyc/cccd`, header `X-API-Key`:

```json
{
  "frontUrl": "https://res.cloudinary.com/your-account/image/upload/front.jpg",
  "backUrl": "https://res.cloudinary.com/your-account/image/upload/back.jpg"
}
```

URL giữ nguyên dạng chuỗi để không biến đổi chữ ký; loader kiểm tra scheme/host/giới hạn ảnh.
DefaultCardPreparer gọi nguyên các module bước 2: tải ảnh → kiểm tra chất lượng → nắn thẻ.
Sau đó thu nhỏ, OCR, gộp box thành dòng, phân loại; nếu UNKNOWN thử xoay 180° khi được bật.
Hai mặt chỉ đổi chỗ khi nhận diện chắc chắn đều CCCD và ngược FRONT/BACK.
Lỗi xử lý từng ảnh có `details[0].image`; sau đổi mặt, tên front/back theo mặt logic được parse.

Response thành công có `idNumber`, `fullName`, `dob`, `gender`, `address`, `placeOfOrigin`,
`expiryDate`, `noExpiry`, `issueDate`, `fieldConfidence`. Ngày là yyyy-MM-dd; bỏ trường null.
Không có field qr. Trường tùy chọn không đọc được trả cảnh báo FIELD_NOT_FOUND;
confidence thấp và lệch cấu trúc ID chỉ cảnh báo, không chặn. Ba trường bắt buộc là số, tên và ngày sinh.
Ngày hết hạn trước hôm nay bị chặn; ngày đúng hôm nay vẫn hợp lệ. Hôm nay lấy theo TIMEZONE.

Parser chỉ sử dụng danh sách nhãn trong đặc tả, giữ nguyên dấu với fold_aligned/NFC.
Giá trị nhiều dòng bị chặn bởi nhãn khác, giới hạn số dòng và khoảng trống bbox dọc.
Dòng nhãn phụ tiếng Anh được bỏ qua; confidence lấy min của các dòng giá trị thực sự dùng.
Ngày cấp mặt sau là ngày hợp lệ đầu tiên theo thứ tự trên xuống, chưa parse MRZ hay QR.
Deadline kiểm tra ngay sau mỗi lần OCR (kể cả lần xoay); không thể ngắt một readtext đang chạy.

Công cụ local (output chứa dữ liệu cá nhân, không chia sẻ công khai):

```powershell
uv run python scripts/try_ocr.py local_samples/front.jpg --side front
uv run python scripts/try_ocr.py local_samples/back.jpg --side back
```

Dùng ảnh đã nắn đúng chiều để chẩn đoán OCR/parser; script không tải URL hay tự chạy bước chất lượng.
`local_samples/`, `tests/fixtures/real/` được ignore. Không có log giá trị trường, text OCR hoặc URL
trong service; HTTP client log INFO được tắt để tránh lộ URL có chữ ký.

Test dùng dữ liệu giả và engine fake; không tải model. Việc Reader/warm-up thật chạy được chỉ
xác minh môi trường, chưa chứng minh độ chính xác OCR trên ảnh CCCD. Bước sau: đối chiếu nhãn/ngưỡng
với ảnh thực tế và triển khai riêng QR, GPLX, face theo yêu cầu tiếp theo.


### Ảnh local bị xoay ngang

`try_ocr.py` mặc định đọc ảnh như đang lưu, không tự xác định hướng chữ 90/270 độ.
`--side` chỉ chọn parser, không xoay ảnh. Chỉ định góc xoay theo chiều kim đồng hồ:

```powershell
uv run python scripts/try_ocr.py tests/data/cccd_front.jpg --side front --rotate 270
```

Tùy chọn `--warp` chạy detector theo `CARD_DETECTOR_BACKEND`, nắn thẻ và quality gate
trước OCR, cùng cấu hình với service. Xem hướng dẫn YOLO bên dưới.
`status=parsed` là trường parse hợp lệ, không đảm bảo nội dung OCR chính xác.


## Bước 3b: gỡ nhãn, cột giá trị và review

Parser dùng LabelSpec tách nhãn Việt và Anh. Chỉ các nhãn Anh dài ít nhất 8 ký tự mới được
scrub; không dùng nhãn Việt để gỡ, không sửa chính tả địa danh. Khi cả thẻ không tìm thấy
nhãn Việt của một trường, nhãn Anh là phương án dự phòng. Không thêm nhãn mới.

Bộ lọc x1 theo nhãn của bước 3b đã được thay bằng mảnh/cột và một lượt duyệt ở bước 3c.
Xem mô tả và cách debug hiện tại bên dưới.

`parsed` thay cho trạng thái parser cũ: chỉ đúng định dạng, không chứng minh nội dung chính xác.
API thêm `reviewFields` (tên camelCase theo thứ tự cố định) và `reviewRequired`, chỉ để tô sáng
trường cần xem kỹ, KHÔNG dùng để tự động duyệt hồ sơ. Thiếu/ngày cấp confidence thấp cũng
được đưa vào review; không thời hạn thì bỏ expiryDate khỏi review. Kiểm tra chéo số CCCD
với năm sinh/giới tính chỉ thêm review/cảnh báo. Response không có score/classificationScore.

Ngưỡng khởi điểm trong `.env.example`: SCRUB_FUZZY_CUTOFF=72,
OCR_REVIEW_CONF=0.85, OCR_REVIEW_CONF_TEXT=0.9.
Cutoff scrub giữ 72 vì các test chống cắt nhầm địa danh đã qua; vẫn cần chủ dự án kiểm chứng
bằng dữ liệu thực tế. Chưa thay engine, thêm lượt OCR hay sửa lỗi dấu/chính tả.

`try_ocr.py` in thêm x1/x2 chuẩn hóa, classify_score (độ khớp từ khóa, không phải độ chính xác OCR),
status/conf/review, debug đoạn gỡ và chỉ số dòng loại cột. Chỉ CLI local nhận ParseDebug;
service không lưu hay log các đoạn OCR. Hàm collect_review_fields được dùng chung giữa CLI và service.
Test bước 3b chỉ dùng dữ liệu giả, không kết luận cải thiện độ chính xác OCR trên ảnh thật.


## Bước 3c: mảnh và hai cột

`OcrLine.parts` giữ box thô để `layout.build_fragments` tách cột mà không OCR lần nữa.
`compute_columns` lấy trung vị x1 của ít nhất CCCD_MIN_ANCHOR_LINES=2 dòng nhãn Việt cột phải
nằm đầu dòng. Nhãn ở giữa dòng gộp không làm mốc. Đường chia cột là
x_right - CCCD_COL_SPLIT_RATIO (mặc định 2.5) × chiều cao trung vị của dòng.

Dòng có parts được chia theo x1 của box; dòng chỉ có một box bên trái có thể tách tại nhãn
cột phải, với estimated=True. Tọa độ dự phòng chỉ là ước lượng, không phải tọa độ detector.
Parser duyệt một lượt mỗi cột; mỗi mảnh giá trị chỉ được dùng một lần, bởi một trường.
Không loại trùng theo nội dung giữa quê quán/địa chỉ. Nhãn phụ vẫn dùng thuật toán cũ.

Khi không đủ mốc hoặc mọi x1 đồng nhất, dùng một cột, layout_reliable=False;
service trả LAYOUT_UNCERTAIN và tô sáng placeOfOrigin/address trong reviewFields.
Giá trị giới tính ngắn như "Nữ" vẫn được nhận, không coi là nhãn phụ chỉ vì ít ký tự.

OCR_WIDTH_THS để trống/không khai báo dùng mặc định EasyOCR; có thể thử 0.2 khi box thô
đã gộp hai cột. CLI cho phép ghi đè theo lần chạy:

```powershell
uv run python scripts/try_ocr.py local_samples/front.jpg --side front --rotate 270 --width-ths 0.2
```

Output: box thô → dòng gộp và số parts → x_right/x_split/layout_reliable → các fragment
(column, source, estimated, tọa độ) → trường và used_fragments → label_splits.
Mọi chỉ số bắt đầu từ 0; source/label_splits trỏ vào danh sách dòng gộp, used_fragments trỏ
vào danh sách mảnh. Debug chỉ dùng ở CLI, chứa dữ liệu cá nhân, không chia sẻ công khai.

Test chỉ dùng dữ liệu giả; chủ dự án cần xác nhận lại bằng ảnh thật. Không kết luận
cải thiện độ chính xác OCR. Engine không đổi ngoài tùy chọn width_ths; không thêm OCR,
không sửa chính tả/địa danh, không đổi schema response ngoài cảnh báo mới.

## Detector YOLO 4 góc và quality gate sau crop

Chi tiết dữ liệu train, đánh giá và giới hạn xem
[hướng dẫn detector](reports/card_detector_2026-11-10/README.md).
Muốn train GPU trên Kaggle và tải trọng số về: [hướng dẫn Kaggle](docs/KAGGLE_CARD_POSE.md),
notebook `notebooks/train_card_pose_kaggle.ipynb`.

Đối chiếu JSON 4 góc với YOLO trên thư mục ảnh mặt trước:

```powershell
uv run python scripts/compare_front_annotations.py tests/data/cccd_front --model models/card_yolov8n_pose.selected.pt --output tests/fixtures/private/front_comparison_new
```

Tool lưu crop PNG, overlay (JSON xanh / YOLO đỏ), OCR và parser/debug theo mã Fxx.
Chỉ nắn JSON khi có đúng một polygon `cccd_front` gồm 4 điểm hợp lệ và đúng kích thước ảnh;
nhãn thiếu/sai thì chạy OCR ảnh gốc để tham khảo. Đây là chẩn đoán, bỏ gate chất lượng và
điều kiện classifier trước parser để xem lỗi; nhánh YOLO vẫn giữ gate hình học.
Không thay OCR/parser hay tự sửa ảnh/JSON nguồn. Dùng thư mục output mới khi đổi ảnh,
nhãn, checkpoint, code hoặc cấu hình để tránh trộn cache.
Model thử nghiệm được train cục bộ, không đưa trọng số/dữ liệu cá nhân vào Git.
Mặc định dự án vẫn là contour + strict; bật YOLO cho phiên PowerShell thử nghiệm:

```powershell
$env:CARD_DETECTOR_BACKEND="yolo"
$env:CARD_YOLO_MODEL_PATH="models/card_yolov8n_pose.selected.pt"
$env:QUALITY_PROFILE="evaluation"
uv run python scripts/try_card_preparer.py tests/data/cccd_front.jpg
uv run python scripts/try_ocr.py tests/data/cccd_front.jpg --side front --warp
uv run uvicorn ekyc.main:app --reload --port 8001
```

`try_card_preparer.py` lưu crop/overlay/quality tại `tests/fixtures/private/card_preview/`,
không chạy OCR. Dùng crop này để kiểm tra mất chữ/thừa nền trước khi đánh giá parser.
Profile evaluation không dùng trong production; trả về cấu hình thường bằng cách xóa
ba biến môi trường trên hoặc mở một phiên PowerShell mới.
