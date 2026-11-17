# eKYC trên Modal

Entry point `scripts/modal_ekyc.py` đưa API trích xuất CCCD/GPLX hiện có lên deployment `ekyc-extraction-staging`. Endpoint dùng Modal Proxy Token. API trích xuất còn kiểm tra `X-API-Key` của eKYC. Giữ cả hai credential ở server hoặc máy kiểm thử; ứng dụng Expo và goAdmin không chứa các khóa.

## Runtime

- CPU 4 core và RAM request 3072 MiB, giới hạn 6144 MiB. Một container, inference chạy tuần tự qua worker giữ sẵn model. Modal có thể nhận thêm một request trong lúc worker bận; queue nội bộ vẫn có giới hạn.
- Workers warm-up khi container khởi động. Giữ container rảnh tối đa 120 giây, rồi scale về 0. Cấu hình này giảm cold start giữa các lần thử gần nhau và giới hạn thời gian trả phí khi rảnh.
- Container đặt ở AP, routing qua AP South. Phiên bản mới dùng một deployment staging riêng.
- Docker chỉ cài core và inference từ `uv.lock`, CPU wheels, một OpenCV headless. Ba weights khoảng 100 MiB được đưa vào image qua ba file rõ tên. Startup kiểm tra SHA-256 rồi mới warm-up, EasyOCR không tải weights khi startup.
- Deadline eKYC vẫn 34 giây mỗi ảnh. Request QR đã giải mã có thể trả đề xuất ngay cả khi bước OCR quá hạn.
- Profile này chưa có cấu hình Firebase hoặc lưu hồ sơ Driver trên Modal. Luồng xác thực và lưu hồ sơ hiện chạy ở môi trường test local.

## Deploy và kiểm tra

Chạy ở `<repo-root>`:

```powershell
uv lock --check
uv run --locked --with modal==1.6.4 modal deploy -e main scripts/modal_ekyc.py
uv run --locked --with modal==1.6.4 modal app logs -e main ekyc-extraction-staging
```

Modal Secret `ekyc-extraction-staging` cần `EKYC_API_KEY`. Smoke test đọc hai file local `ekyc-key.env` và `proxy-token.json` trong `%LOCALAPPDATA%\EkycModalStaging`, gửi ảnh tổng hợp và chỉ in HTTP status, thời gian và mã kết quả.

```powershell
uv run --locked python scripts/modal_latency_smoke.py https://<modal-workspace>--ekyc-extraction-staging-cccd-api.ap-south.modal.run
```

Để đo cold candidate, chờ hơn 120 giây không có request, chạy lại với `--cold-candidate` và đối chiếu timestamp startup trong Modal logs. Vài mẫu smoke không đại diện cho p95/p99. Khối lượng ảnh, thời gian upload và chất lượng ảnh thực tế ảnh hưởng đến latency. Dùng endpoint test với ảnh tổng hợp.

Rollback bằng cách chọn phiên bản trước trong lịch sử deployment hoặc dừng app test. Giữ nguyên local eKYC, Expo và goAdmin khi kiểm tra Modal.


## Kết quả smoke trên Modal

- Deployment chạy ở workspace `<modal-workspace>`, environment `main`, app `ekyc-extraction-staging`. Proxy trả HTTP 401 khi thiếu token. Sau xác thực, readiness trả HTTP 200 và worker `warm`.
- Readiness cold đầu tiên mất **17,41 giây** từ máy kiểm thử. Log xác nhận thực sự có startup và warm-up worker mất **8,89 giây**. Warm health median **142 ms** trên ba lượt.
- Ba request ảnh tổng hợp 560×350 trả HTTP 200 sau **0,86–1,12 giây**. Ảnh tổng hợp 1600×1200 trả HTTP 200 sau **7,47 giây**. Log đo OCR bên trong khoảng **7,01 giây** trên ảnh lớn. Các ảnh kiểm thử không có trường hợp lệ để chấm accuracy.
- Peak RSS worker quan sát được **2731 MiB**, không đại diện peak toàn container. Image Docker local trước khi gắn weights khoảng **600 MB compressed**. Ba file weights thêm khoảng **100 MiB** theo kích thước file gốc. Chưa đo kích thước lưu trữ cuối trong hệ thống image của Modal.
- Builder Modal cài bằng `--frozen` từ lock đã kiểm tra locally do remote lock validation báo cần refresh. Docker mặc định vẫn `--locked`. Modal bỏ qua `USER` và `HEALTHCHECK` của Dockerfile, dùng ASGI readiness để kiểm chứng rollout.

## CI/CD lên Modal qua GitHub Actions

Workflow [deploy-modal.yml](../.github/workflows/deploy-modal.yml) triển khai thủ công (`workflow_dispatch`, không tự chạy khi push) cho `scripts/modal_ekyc.py`, giữ nguyên App name `ekyc-extraction-staging`, ASGI endpoint `cccd_api` (`requires_proxy_auth=True`), cấu hình `cpu=4`, `memory=(3072, 6144)`, `min_containers=0`, `max_containers=1`, `scaledown_window=120`, `region="ap"`, `routing_region="ap-south"`, pipeline inference và toàn bộ ngưỡng xác minh.

- Hiện tại chưa cấu hình Modal `production` tách biệt, nên workflow **chỉ cho phép chọn `staging`** (mặc định trỏ Modal Environment `main`). Khi cần mở `production`, phải tạo Modal Environment `production`, Modal Secret `ekyc-extraction-production` và Modal Volume `ekyc-models-prod` riêng, đồng thời chỉ cho phép dispatch từ `refs/heads/main`.
- Job `quality-gates` chạy `uv lock --check`, `uv sync --locked`, Ruff, Pytest, JS tests, `uv audit --locked` và kiểm tra entrypoint/manifest offline mà không có credentials Modal.
- Job `deploy` gắn `concurrency` theo môi trường, dùng GitHub Environment tương ứng (`staging` hoặc `production`), chỉ cấp `MODAL_TOKEN_ID` và `MODAL_TOKEN_SECRET` cho đúng bước `Deploy to Modal`, và gắn tag `--tag sha-<12-ký-tự>` kèm `APP_VERSION` / `GIT_COMMIT_SHA`.

### 1. Tạo Modal Token và Modal Proxy Token

1. Đăng nhập Modal Dashboard → **Settings → API Tokens → New Token** để lấy cặp `MODAL_TOKEN_ID` và `MODAL_TOKEN_SECRET` cho workspace triển khai.
2. Tại **Settings → Proxy Auth Tokens** (hoặc trong App settings vì `cccd_api` bật `requires_proxy_auth=True`), tạo cặp `Modal-Key` (`MODAL_PROXY_KEY`) và `Modal-Secret` (`MODAL_PROXY_SECRET`) cho bước kiểm chứng sau deploy.

### 2. Cấu hình GitHub Environments, Secrets và Variables

Tạo hai GitHub Environment riêng biệt: `staging` và `production` (khuyến nghị bật *Required reviewers* và *Deployment branches: main* cho `production`).

| Loại | Tên | Mục đích | Phạm vi cấp phát |
| --- | --- | --- | --- |
| Secret | `MODAL_TOKEN_ID` | Xác thực Modal CLI để deploy | Chỉ bước `Deploy to Modal` |
| Secret | `MODAL_TOKEN_SECRET` | Xác thực Modal CLI để deploy | Chỉ bước `Deploy to Modal` |
| Secret | `MODAL_PROXY_KEY` | Header `Modal-Key` kiểm tra endpoint private | Chỉ bước `Post-deploy verification` |
| Secret | `MODAL_PROXY_SECRET` | Header `Modal-Secret` kiểm tra endpoint private | Chỉ bước `Post-deploy verification` |
| Secret | `EKYC_SMOKE_API_KEY` | Giá trị kiểm thử khớp `EKYC_API_KEY` của env đó để test `X-API-Key` và smoke ảnh tổng hợp | Chỉ bước `Post-deploy verification` |
| Variable | `MODAL_ENVIRONMENT` | Tên Modal Environment (`main` cho staging, `production` cho prod) | Deploy & Verify |
| Variable | `MODAL_APP_NAME` | Mặc định `ekyc-extraction-staging` | Deploy & Verify |
| Variable | `MODAL_MODEL_VOLUME_NAME` | Mặc định `ekyc-models` | Deploy & Verify |
| Variable | `MODAL_SECRET_NAME` | Tên Modal Secret chứa `EKYC_API_KEY` (mặc định `ekyc-extraction-staging`) | Deploy |
| Variable | `MODAL_EXTRA_SECRETS` | Danh sách tên Modal Secret bổ sung phân cách dấu phẩy (tùy chọn) | Deploy |

### 3. Modal Secrets và tách biệt Staging / Production

Giữ toàn bộ runtime secrets trong **Modal Secrets** theo từng Modal Environment (`-e main` hoặc `-e staging` vs `-e production`), tuyệt đối không đưa vào source GitHub hoặc in ra log:

- **Bắt buộc:** `EKYC_API_KEY` (chuỗi ngẫu nhiên >= 32 ký tự).
- **Tùy chọn (chỉ thêm khi bật workflow hồ sơ / đồng bộ backend):** `VERIFICATION_SYNC_KEY` (>= 32 ký tự), `VERIFICATION_STORAGE_KEY` (Fernet key), `FIREBASE_PROJECT_ID`, `GOOGLE_APPLICATION_CREDENTIALS` (JSON service account mount qua Modal Secret), `VERIFICATION_DB_PATH`, `VERIFICATION_STORAGE_DIR`.
- **Tách biệt Staging / Production:** Staging dùng `ENV=staging` và Modal Environment riêng (`main` hoặc `staging`), Production dùng `ENV=prod` (tự động tắt `/docs`, `/redoc`, `/openapi.json`), Modal Environment `production`, Modal Secret riêng và Modal Volume riêng (`ekyc-models-prod` nếu tách kho model).

### 4. Quản lý và khôi phục Model trong Modal Volume

Trong CI (`MODAL_MODEL_SOURCE=volume`), `scripts/modal_ekyc.py` mount Modal Volume ở chế độ **chỉ đọc** (`create_if_missing=False`, `.read_only()`) tại `/opt/ekyc`. CI **không tự upload, không tạo mới, không xóa và không ghi đè** weights đang chạy.

Cấu trúc chuẩn bên trong Modal Volume (`ekyc-models`):

| Đường dẫn trong Volume | Artifact | SHA-256 bắt buộc |
| --- | --- | --- |
| `/models/card_pose_v3_best.pt` | YOLO Pose weights | `ea75a70de1cab77d0ffbe1d56109f2d7046ea059f1fbfa3fe32f270cb23d377e` |
| `/ocr/craft_mlt_25k.pth` | EasyOCR CRAFT | `4a5efbfb48b4081100544e75e1e2b57f8de3d84f213004b14b85fd4b3748db17` |
| `/ocr/latin_g2.pth` | EasyOCR Latin G2 | `aaa95be1c4a9cb3496879bed7c520886ce1164f89e026f0c54488394e74e8c55` |
| `/face/face_detection_yunet_2023mar.onnx` *(tùy chọn)* | YuNet ONNX (chỉ local; tắt ở `staging`/`prod`) | `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4` |
| `/face/face_recognition_sface_2021dec.onnx` *(tùy chọn)* | SFace ONNX (chỉ local; tắt ở `staging`/`prod`) | `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79` |

Khôi phục hoặc chuẩn bị Volume lần đầu từ máy quản trị đã kiểm tra SHA-256 cục bộ (chỉ chạy thủ công, không chạy trong CI):

```powershell
# 1. Tạo volume nếu chưa có và tải 3 weights đã đối chiếu SHA-256
uv run --locked --with modal==1.6.4 python -m modal volume create -e main ekyc-models
uv run --locked --with modal==1.6.4 python -m modal volume put -e main ekyc-models models/card_pose_v3_best.pt /models/card_pose_v3_best.pt
uv run --locked --with modal==1.6.4 python -m modal volume put -e main ekyc-models "$env:USERPROFILE\.EasyOCR\model\craft_mlt_25k.pth" /ocr/craft_mlt_25k.pth
uv run --locked --with modal==1.6.4 python -m modal volume put -e main ekyc-models "$env:USERPROFILE\.EasyOCR\model\latin_g2.pth" /ocr/latin_g2.pth

# 2. Kiểm tra lại cấu trúc volume (chỉ đọc)
uv run --locked --with modal==1.6.4 python scripts/modal_pre_deploy_check.py --offline --check-volume
```

Khi khởi động container, `verify_models` kiểm tra SHA-256 của cả 3 weights và version package trước khi `warm_up()`. Nếu thiếu file hoặc sai checksum, `/health/ready` trả HTTP `503` kèm `MODEL_FILE_MISSING` hoặc `MODEL_HASH_MISMATCH`.

### 5. Chạy workflow, kiểm tra deployment và xem logs

1. Trên GitHub → **Actions → Deploy eKYC to Modal → Run workflow** → chọn `staging` hoặc `production` (chỉ từ `main`).
2. Bước `Post-deploy verification` chạy `scripts/modal_post_deploy_verify.py` để xác minh:
   - Chặn truy cập chưa xác thực proxy (`401`).
   - `GET /health` trả `200`, khớp `commitSha` và `version` (`sha-...`).
   - `GET /health/ready` trả `200`, `status: "ready"`, `worker: "warm"`.
   - Internal endpoints (`/ekyc/metrics`, `/ekyc/documents/cccd_front/extract`) trả `401` khi thiếu/sai `X-API-Key` và `200` khi hợp lệ.
   - Smoke test bằng ảnh PNG tổng hợp (`560×350`, `"SYNTHETIC TEST"`, không dùng ảnh CCCD/GPLX/selfie thật) trả HTTP `200`.
3. Kiểm tra lịch sử version và logs bằng Modal CLI:

```powershell
uv run --locked --with modal==1.6.4 python -m modal app history -e main ekyc-extraction-staging
uv run --locked --with modal==1.6.4 python -m modal app logs -e main ekyc-extraction-staging
```

### 6. Rollback an toàn

Rollback chỉ đưa **application code / container image** về phiên bản trước, **không tự động rollback dữ liệu hay weights trong Modal Volume**:

1. **Kiểm tra tương thích trước khi rollback:**
   - Kiểm tra xem phiên bản app cũ có dùng cùng 3 model SHA-256 trong `ekyc-models` không (`CARD_MODEL`, `EASYOCR_MODELS`).
   - Nếu bật persistent verification storage, kiểm tra tương thích envelope `E1`/`D1` (`VERIFICATION_STORAGE_KEY`) và schema SQLite; tạm ngừng ghi nếu có thay đổi schema trước khi chuyển về bản cũ.
2. **Rollback qua Modal CLI:**
   ```powershell
   # Xem danh sách version và tag sha-... trước đó
   uv run --locked --with modal==1.6.4 python -m modal app history -e production ekyc-extraction-staging
   # Quay về bản ngay trước đó (hoặc chỉ định VERSION cụ thể như v3)
   uv run --locked --with modal==1.6.4 python -m modal app rollback -e production ekyc-extraction-staging
   ```
3. **Rollback qua Modal Dashboard:** Mở App `ekyc-extraction-staging` đúng Environment → tab **Deployments / History** → chọn phiên bản ổn định trước đó → **Rollback**.
4. Chạy lại `scripts/modal_post_deploy_verify.py` (không truyền `--expected-sha` của bản lỗi) để xác nhận `/health/ready` trả `200` sau rollback.
