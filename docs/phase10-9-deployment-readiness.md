# Phase 10.9 — Chuẩn bị triển khai và vận hành

Giữ các route inference `/ekyc/documents/*`, `/ekyc/face/evaluate`, `/ekyc/cccd/*` và metrics trên mạng riêng của backend. Giữ cổng Compose 8001 trên loopback và cấp `X-API-Key` qua secret nội bộ cho backend. Nếu ingress phục vụ workflow xác minh độc lập, chỉ định danh sách route công khai cần dùng và kiểm tra TLS, quyền Firebase cùng rate limit trên staging.

Backend nhận tối đa 8.000.000 byte cho từng ảnh capture. FastAPI kiểm tra giới hạn ảnh khi đọc luồng, còn face evaluation giới hạn toàn bộ JSON trước khi parse theo kích thước base64 của tối đa ba ảnh. Cấu hình reverse proxy theo cùng giới hạn cho các tuyến tương ứng, tính cả overhead multipart nếu tuyến đó dùng multipart. Đặt quota kết nối và timeout ở ingress. Tài liệu này không xác nhận cấu hình ingress đang chạy.

## Phạm vi và môi trường

| ENV | Mục đích | Xác thực và dữ liệu |
| --- | --- | --- |
| `dev` | API và kiểm thử cục bộ | Dùng fixture tổng hợp và cấu hình riêng |
| `demo` | Driver capture demo cục bộ, entry point riêng | Demo token riêng, fixture tổng hợp |
| `staging` | Smoke test và tích hợp trong môi trường tách biệt | API key ngẫu nhiên tối thiểu 32 ký tự, TLS cho xác minh tài xế, Firebase staging khi bật workflow |
| `prod` | Cấu hình dành cho đợt triển khai riêng | Các ràng buộc của staging, tài liệu API HTTP tắt, danh sách admin origin HTTPS chính xác |

Staging và production yêu cầu profile chất lượng strict, từ chối cấu hình thử nghiệm face, geometry, keypoint recovery và OCR factory. Driver API xác minh Firebase UID và quyền admin từ backend. Khi bật workflow xác minh, cấu hình đồng thời Firebase project và SQLite. Storage tài liệu có kiểm tra quyền, encryption bắt buộc và đường tải ảnh qua endpoint được ủy quyền. Chỉ đưa dữ liệu tổng hợp qua local smoke test.

## Dependency và model release

`pyproject.toml` khai báo core runtime và các nhóm `inference`, `face`, `training`, `benchmark`, `dev`. Docker dùng `uv sync --locked --no-default-groups --group inference`; nhóm face tái sử dụng OpenCV headless và mặc định tắt chức năng. Máy phát triển cài các nhóm phục vụ script hiện có theo mặc định. Phiên bản Ultralytics giữ nguyên, dùng bản phân phối headless để cùng EasyOCR cài duy nhất `opencv-python-headless`.

| Artifact | Release | SHA-256 |
| --- | --- | --- |
| `card_pose_v3_best.pt` | card-pose-v3-best | `ea75a70de1cab77d0ffbe1d56109f2d7046ea059f1fbfa3fe32f270cb23d377e` |
| `craft_mlt_25k.pth` | EasyOCR 1.7.2 CRAFT | `4a5efbfb48b4081100544e75e1e2b57f8de3d84f213004b14b85fd4b3748db17` |
| `latin_g2.pth` | EasyOCR 1.7.2 latin-g2 | `aaa95be1c4a9cb3496879bed7c520886ce1164f89e026f0c54488394e74e8c55` |

Các phiên bản inference được kiểm tra trước khi warm-up. Cache weights được chuẩn bị trước và mount chỉ đọc; EasyOCR tắt download tự động. Chỉ dùng artifact đã biết nguồn gốc, so SHA-256 trước khi chuyển sang staging. Cache thư viện ghi vào `/tmp` và mất khi container bị xóa. Chạy `python -m ekyc.services.model_artifacts` trong staging để kiểm tra lại; `--packages-only` phục vụ CI chưa có weights. Khi thiếu hoặc sai artifact, `/health` vẫn trả 200, `/health/ready` trả 503 cùng mã như `MODEL_FILE_MISSING` hoặc `MODEL_HASH_MISMATCH`. Khi warm-up lỗi, readiness trả `MODEL_WARMUP_FAILED`. Sửa cấu hình hoặc artifact rồi restart.

## Local và staging trên Windows

```powershell
Set-Location <repo-root>
uv sync --locked
uv run ruff check .
uv run pytest -q
node --test frontend/driver-capture/*.test.mjs
uv audit --locked --no-group training --no-group benchmark --no-group dev
```

Để chạy API dev, chuẩn bị `.env` từ `.env.example`, cấu hình key dev và cache weights rồi chạy `uv run uvicorn ekyc.main:app --host 127.0.0.1 --port 8001`. Với staging cục bộ, dùng file key riêng và chỉ bind loopback.

```powershell
Copy-Item .env.staging.example .env.staging
$stagingKey = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
Set-Content .env.staging "EKYC_API_KEY=$stagingKey"
Remove-Variable stagingKey
$env:EKYC_YOLO_FILE = (Resolve-Path models/card_pose_v3_best.pt).Path
$env:EKYC_OCR_DIR = (Resolve-Path (Join-Path $env:USERPROFILE '.EasyOCR/model')).Path
$env:EKYC_STAGING_PORT = '8001'
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
Invoke-RestMethod http://127.0.0.1:8001/health
Invoke-RestMethod http://127.0.0.1:8001/health/ready
docker compose exec ekyc python -m ekyc.services.model_artifacts
docker compose stop
```

Các lệnh tạo key dành cho lần thiết lập đầu. Giữ key staging trong secret manager khi tích hợp môi trường thật. Biến `EKYC_ENV_FILE` cho phép chọn file cấu hình khác. Compose tự đặt `ENV`, `OCR_BACKEND`, `CARD_DETECTOR_BACKEND`, `CARD_YOLO_MODEL_PATH`, `EASYOCR_MODEL_DIR` và warm-up. Compose dùng 4 CPU, 8 GiB, 256 PID, root filesystem chỉ đọc, UID 10001, không capabilities, 45 giây chờ dừng. Uvicorn dùng một process và 35 giây graceful timeout. Duy trì readiness probe riêng với liveness.

Để tích hợp workflow tài xế, cấu hình `FIREBASE_PROJECT_ID` của staging, Firebase Application Default Credentials của staging, `VERIFICATION_DB_PATH`, `VERIFICATION_STORAGE_DIR`, `VERIFICATION_STORAGE_KEY` là Fernet key quản lý riêng, và `VERIFICATION_ADMIN_ORIGINS` là mảng JSON các origin được phép. Mount DB và storage vào volume riêng mà UID 10001 có quyền ghi, dùng ổ đĩa mã hóa cho DB/metadata và backup. Dùng reverse proxy TLS quản lý header forwarding; chỉ đặt `FORWARDED_ALLOW_IPS` cho địa chỉ proxy thực, giới hạn đường truy cập metrics, đặt giới hạn upload và tắt log query/body/header chứa thông tin nhạy cảm. Kiểm tra credential, quyền admin và quyền preview bằng tài khoản staging. Docker smoke mặc định chạy không có Firebase hoặc persistent workflow storage.

`scripts/modal_ekyc.py` tái sử dụng Dockerfile với cấu hình staging và chỉ thêm ba weights vào image khi một đợt chạy Modal được phê duyệt. Smoke cục bộ dùng Docker. Modal có cơ chế chạy container bằng root và bỏ qua Docker `USER`, nên kết quả xác minh non-root chỉ áp dụng Docker. [Modal Dockerfile behavior](https://modal.com/docs/guide/existing-images).

## Quan sát và CI

- `X-Request-ID` cho đối chiếu phản hồi với log. Staging/production tạo ID phía server. Uvicorn access log tắt trong Docker. Giữ payload, token, transcript và danh tính ngoài metric labels.
- `GET /ekyc/metrics` yêu cầu `X-API-Key` và trả `Cache-Control: no-store`. Scrape trong mạng quản trị. Các gauge gồm queue depth, worker active (0/1) và worker peak RAM MiB từ mẫu runtime. Các counter gồm kết quả worker, timeout, restart, HTTP status class và lỗi theo stage/mã. Tính tỷ lệ business error bằng counter `business_error` chia tổng counter theo HTTP status class trên cùng cửa sổ. Không cộng lại bucket `business_error` vào mẫu số.
- p50/p95/p99 là nearest-rank trong tối đa 2048 mẫu gần nhất của mỗi process. Dùng để theo dõi từng instance. Các stage I/O, QR, detector, warp, OCR, parser và khởi tạo có trong structured timing log khi được chạy. Nếu cần tổng hợp nhiều instance, thu thập phân phối ở collector thay vì cộng các percentile.
- GitHub Actions kiểm tra lockfile, Ruff, Pytest, test Node của demo, audit dependency, Docker build, package integrity và staging readiness fail-closed khi thiếu weights. Expo và web admin nằm ở repo khác.
- Theo dõi readiness 503 kéo dài, queue tăng liên tục, timeout/restart và peak RAM so với cgroup memory. Trước khi tăng replicas cho workflow lưu SQLite, xác minh kiến trúc lưu dữ liệu dùng chung hỗ trợ truy cập đồng thời.

## Migration, backup và rollback

1. Chốt image digest, lockfile và SHA của artifact. Kiểm tra quyền đọc weights, quyền ghi volume, TLS, Firebase staging, khóa lưu trữ, health/readiness và alert.
2. Nếu có dữ liệu cần chuyển, quiesce submit/upload/admin writes và retention; chụp SQLite bằng online backup API hoặc snapshot nhất quán, sao lưu kèm thư mục object storage cùng mốc thời gian. Giữ access log của backup ở phạm vi vận hành, mã hóa backup và quản lý Fernet key riêng.
3. Khôi phục trên volume staging tách biệt và chạy `PRAGMA integrity_check`, xác minh đọc ảnh được ủy quyền cùng revision. Kiểm tra startup với bản sao trước khi trỏ instance sang dữ liệu. Schema hiện được tạo tại startup đối với DB mới; driver demo SQLite có schema và workflow riêng, không dùng trực tiếp làm DB API chính.
4. Khi rollback, dừng writes, xác minh khả năng tương thích schema rồi dùng image digest và artifact bundle đã chạy ổn định trước đó. Nếu schema không tương thích, khôi phục bản DB/storage đồng bộ trước migration và đối soát các ghi phát sinh. Tắt retention trong thời gian khôi phục.
5. Trước khi mở staging cho người kiểm thử, thử submit/resubmit với fixture tổng hợp, admin authorization, unauthorized image access, timeout, cảnh báo queue và backup restore. Việc chuyển tiếp production là một đợt phê duyệt riêng.


## Kết quả kiểm tra cục bộ

- Docker Desktop build được image Linux; Docker Compose warm-up, nhận ảnh PNG tổng hợp qua endpoint trích xuất, bảo vệ metrics bằng API key và shutdown sạch. Xác nhận UID 10001 và thư mục ứng dụng chỉ chứa runtime venv.
- Container chạy không có mạng ngoài đã khởi tạo YOLO và EasyOCR từ ba weights đủ SHA-256. Container thiếu weights trả `MODEL_FILE_MISSING` từ readiness.
- Hai request tổng hợp offline trả HTTP 200 ở khoảng 1,27 giây (300×180) và 23,09 giây (1600×1200). Một lần chạy ảnh 1600×1200 trước đó gặp HTTP 504 khi máy cũng đang thực hiện kiểm tra/build. Các phép thử này chưa đo thông lượng hoặc p95 đại diện. Giữ nguyên deadline 25 giây và đo tải thực tế trước khi mở staging rộng hơn.
- Ruff, kiểm tra lockfile, Pytest 458 ca, Node 17 ca và compileall đạt. `uv audit` không tìm thấy advisory trong kết quả tại thời điểm chạy. Audit này bao phủ dependency Python theo lockfile; chưa quét CVE của OS layer.
- Import entry point Modal bằng SDK cục bộ đạt. GitHub Actions, Modal từ xa, Firebase staging dưới TLS và restore dữ liệu trên volume staging còn cần xác minh trong môi trường tương ứng.
