# Train góc thẻ trên Kaggle, tải model về dùng local

Đã chuẩn bị:

- Notebook: `F:/ekyc-service/notebooks/train_card_pose_kaggle.ipynb`.
- ZIP để upload: `F:/ekyc-service/tests/fixtures/private/card_pose_kaggle.zip`.
- Gói gồm 37 ảnh train + 16 ảnh validation, nhãn 4 góc, code train tối thiểu và model seed hiện tại.
  Không chứa `.env`, khóa API, OCR/parser, toàn bộ service hay lockfile.

Ảnh vẫn chứa dữ liệu cá nhân dù tên file là Fxx/Bxx. Giữ **Dataset và Notebook Private**;
chỉ đưa lên Kaggle dữ liệu bạn được phép xử lý ở đó. Chưa upload gì từ máy của bạn.

## 1. Upload dữ liệu

1. Đăng nhập Kaggle, mở **Datasets → New Dataset / Create Dataset**.
2. Chọn ZIP `card_pose_kaggle.zip` ở trên.
3. Chọn quyền truy cập **Private**, đặt tên, tạo dataset.

Kaggle có thể giải nén ZIP khi tạo dataset. Notebook hỗ trợ cả thư mục đã giải nén
và file ZIP còn nguyên. Không upload cả thư mục project.

## 2. Mở notebook, bật GPU

1. Tạo notebook mới trong Kaggle.
2. **File → Import Notebook**, chọn `train_card_pose_kaggle.ipynb`.
3. **Add Input / Add Data**, thêm dataset private vừa tạo.
4. **Settings → Accelerator → GPU**, chọn loại GPU đang khả dụng.
5. Bật **Internet** để cài các thư viện ở cell đầu.
6. Giữ notebook **Private**, chạy các cell theo thứ tự hoặc **Run All**.

Cell kiểm tra phải in tên GPU và `torch.cuda.is_available()` phải là True.
Notebook dùng `device=0`, tức GPU đầu tiên; không cần dùng cả hai GPU nếu được cấp T4 x2.

**Không chạy `uv sync` hoặc `pip install .` cả service trên Kaggle.**
Project hiện chọn Torch CPU trên Linux. Notebook dùng Torch CUDA có sẵn của Kaggle,
chỉ cài Ultralytics 8.4.208 và các thư viện cần cho train/chọn checkpoint.

## 3. Train

Notebook tự kiểm tra hash bundle, sửa `path` trong YAML từ Windows sang Linux và đặt
`PYTHONPATH` cho code tối thiểu. Dataset/code/seed nằm trong `/tmp`; kết quả train nằm
trong `/kaggle/working`.

Cell train mặc định:

```python
EPOCHS = 120
BATCH = 16
IMGSZ = 640
```

Đây là fine-tune từ model đã train cục bộ, không phải train từ đầu. Nếu thiếu VRAM,
giảm BATCH xuống 8 hoặc 4. Các tham số learning rate/augmentation chỉ là cấu hình khởi đầu,
không đảm bảo model mới tốt hơn. GPU giúp rút ngắn thời gian; không khắc phục nhãn sai
hoặc bộ train chỉ có một thẻ. Giữ nhóm thẻ tách biệt giữa train và validation khi thêm data.

Model YOLO chỉ nhận diện 4 góc. Bước warp perspective và xoay ngang vẫn chạy trong service
ở máy local sau khi nạp tọa độ model; không có model warp riêng cần tải về.

Script tự so sánh `best.pt` và `last.pt` trên validation bằng sai số góc, phạt trường hợp
không qua kiểm tra hình học. Chọn checkpoint chạy CPU để cùng kiểu kiểm tra với service.
Không dùng mAP như độ chính xác OCR. Validation dùng chọn model không phải test độc lập.

## 4. Tải kết quả

Nếu chạy trực tiếp bằng Run All, sau khi các cell cuối hoàn tất tải file qua link cuối
notebook hoặc Output của phiên hiện tại. Có thể dùng **Save Version → Save & Run All**
ngay từ đầu để chạy nền và lưu một lượt đầy đủ; cách này thực thi lại notebook, nên không
cần chọn lại sau Run All chỉ để tải file. Mở **Output** của lượt đã chạy và tải:

```text
card_detector_download.zip
```

ZIP tải về chỉ gồm:

- `card_yolov8n_pose.kaggle.pt`: trọng số được chọn, dùng cho service.
- Các file JSON: thông số train, chọn checkpoint, hash/provenance.
- `results.csv`, `args.yaml`: lịch sử và cấu hình train.

Không cần tải lại ảnh train. Thư mục `card_pose_training` trong Output có checkpoint
của từng lượt, bao gồm `last.pt`; giữ lại nếu cần chạy tiếp sau này.
Tắt session GPU khi hoàn tất.

## 5. Dùng model trên Windows

Giải nén ZIP vào `F:/ekyc-service/models/kaggle/`, giữ model local cũ để so sánh.
Trong PowerShell tại `F:/ekyc-service`:

```powershell
$env:CARD_DETECTOR_BACKEND="yolo"
$env:CARD_YOLO_MODEL_PATH="F:/ekyc-service/models/kaggle/card_yolov8n_pose.kaggle.pt"
$env:QUALITY_PROFILE="evaluation"

uv run python scripts/evaluate_card_preparer.py tests/fixtures/private/card_pose_dataset/annotations.json --model models/kaggle/card_yolov8n_pose.kaggle.pt --output tests/fixtures/private/card_pose_dataset/kaggle_eval
```

Xem `results.json`, `summary.json`, các file `*_overlay.jpg` và `*_crop.jpg` ở
`F:/ekyc-service/tests/fixtures/private/card_pose_dataset/kaggle_eval/`.
So sánh với model local: 48/55 qua hình học, 43/55 qua profile evaluation;
đồng thời xem mất chữ, lệch góc và giữ thừa nền, không chỉ đếm ảnh qua gate.

Chạy service với model mới:

```powershell
uv run uvicorn ekyc.main:app --reload --port 8001
```

Model `.pt` train GPU vẫn được detector hiện tại chạy inference CPU. Không cần export ONNX.
Muốn giữ cấu hình qua các phiên PowerShell, sửa ba khóa tương ứng trong `.env`;
profile evaluation chỉ dành cho thử nghiệm, không dùng với `ENV=prod`.

## 6. Đóng gói lại sau khi sửa nhãn/thêm ảnh

Rà nhãn trong `F:/ekyc-service/tests/fixtures/private/card_pose_dataset/export/annotation_review/`.
Với dataset export mới, tạo gói mới (script không ghi đè ZIP đã có):

```powershell
uv run python scripts/package_card_pose_kaggle.py --dataset tests/fixtures/private/card_pose_dataset/export_v2 --output tests/fixtures/private/kaggle_v2/card_pose_kaggle.zip
```

Cập nhật dataset Kaggle, gắn đúng phiên bản vào notebook và train lại. Nếu đã thêm ảnh/đổi
nhãn, dùng exporter của project để tạo `images`, `labels`, `data.yaml`, `manifest_summary.json`
đồng nhất trước khi đóng gói.

Nguồn thao tác GPU: [NVIDIA: Kaggle GPU notebook](https://docs.nvidia.com/datascience/deployment/26.10/platforms/kaggle/).
Tham số train/device: [Ultralytics Train](https://docs.ultralytics.com/modes/train/).
Giao diện dataset/notebook: [Kaggle Datasets](https://www.kaggle.com/docs/datasets),
[Kaggle Notebooks](https://www.kaggle.com/docs/notebooks).
