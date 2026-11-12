import os

import cv2
import numpy as np
from ultralytics import YOLO

# ================= CẤU HÌNH =================
MODEL_PATH = "./models/best.pt"  # Đường dẫn tới model V2 của bạn
TEST_DIR = "./card_dataset/images/test"  # Thư mục chứa ảnh test (gồm cả thẻ và rác)
OUTPUT_VIS_DIR = "./test_results_visual"  # Thư mục lưu ảnh đã vẽ
OUTPUT_TXT_DIR = "./test_results_data"  # Thư mục lưu số liệu (.txt)
# ============================================


def order_points_clockwise(pts):
    """Sắp xếp 4 điểm: Trái-Trên (TL), Phải-Trên (TR), Phải-Dưới (BR), Trái-Dưới (BL)"""
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]

    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


def main():
    os.makedirs(OUTPUT_VIS_DIR, exist_ok=True)
    os.makedirs(OUTPUT_TXT_DIR, exist_ok=True)

    print(f"Đang tải mô hình từ {MODEL_PATH}...")
    model = YOLO(MODEL_PATH)

    test_images = [f for f in os.listdir(TEST_DIR) if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    print(f"Bắt đầu kiểm thử trên {len(test_images)} ảnh...")

    count_detect = 0
    count_empty = 0

    for img_name in test_images:
        img_path = os.path.join(TEST_DIR, img_name)
        img = cv2.imread(img_path)

        # Dự đoán với ngưỡng tự tin 70% để lọc rác
        results = model.predict(img_path, conf=0.7, verbose=False)

        txt_path = os.path.join(OUTPUT_TXT_DIR, os.path.splitext(img_name)[0] + ".txt")
        out_img_path = os.path.join(OUTPUT_VIS_DIR, img_name)

        # 1. Xử lý Ảnh Âm (Không có thẻ)
        if len(results[0].boxes) == 0:
            # Ghi log số liệu
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("[TRUE NEGATIVE] Không phát hiện thẻ nào. Mô hình bỏ qua ảnh này.\n")

            # Đánh dấu trực quan
            cv2.putText(
                img, "NO CARD DETECTED", (30, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2
            )
            cv2.imwrite(out_img_path, img)
            count_empty += 1
            continue

        count_detect += 1

        # 2. Xử lý Ảnh có thẻ
        with open(txt_path, "w", encoding="utf-8") as f:
            # Lặp qua tất cả thẻ tìm được trong 1 ảnh (thường là 1 thẻ)
            for i, (box, kpts) in enumerate(
                zip(results[0].boxes, results[0].keypoints, strict=False)
            ):
                # --- TRÍCH XUẤT SỐ LIỆU ---
                conf = float(box.conf[0])
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                raw_kpts = kpts.xy[0].cpu().numpy()

                if len(raw_kpts) == 4:
                    ordered_kpts = order_points_clockwise(raw_kpts)
                else:
                    ordered_kpts = raw_kpts

                # Xuất số liệu ra file txt
                f.write(f"--- ĐỐI TƯỢNG {i + 1} ---\n")
                f.write(f"Độ tự tin (Confidence): {conf * 100:.2f}%\n")
                f.write(f"Bounding Box (x_min, y_min, x_max, y_max): {x1}, {y1}, {x2}, {y2}\n")
                f.write("Tọa độ 4 góc (Trái-Trên, Phải-Trên, Phải-Dưới, Trái-Dưới):\n")
                for pt in ordered_kpts:
                    f.write(f"  - ({int(pt[0])}, {int(pt[1])})\n")
                f.write("\n")

                # --- VẼ TRỰC QUAN (OPENCV dùng BGR) ---
                # A. Vẽ Bounding Box (Màu Xanh lá - Green)
                cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
                cv2.putText(
                    img,
                    f"Conf: {conf:.2f}",
                    (x1, max(10, y1 - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 0),
                    2,
                )

                if len(raw_kpts) == 4:
                    # B. Vẽ Đường nối 4 góc (Màu Xanh dương - Blue)
                    pts_int = np.int32(ordered_kpts).reshape((-1, 1, 2))
                    cv2.polylines(img, [pts_int], isClosed=True, color=(255, 0, 0), thickness=2)

                    # C. Vẽ 4 Điểm góc (Màu Đỏ - Red) và dán nhãn
                    labels = ["TL", "TR", "BR", "BL"]
                    for _j, (pt, label) in enumerate(zip(ordered_kpts, labels, strict=False)):
                        px, py = int(pt[0]), int(pt[1])
                        cv2.circle(img, (px, py), 6, (0, 0, 255), -1)  # Chấm đỏ đặc
                        cv2.putText(
                            img,
                            label,
                            (px + 10, py - 10),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.6,
                            (0, 0, 255),
                            2,
                        )

        cv2.imwrite(out_img_path, img)

    print("\nHoàn tất kiểm thử!")
    print(f"- Số ảnh phát hiện thẻ: {count_detect}")
    print(f"- Số ảnh bỏ qua (Negative): {count_empty}")
    print(f"Vui lòng kiểm tra thư mục '{OUTPUT_VIS_DIR}' (Ảnh) và '{OUTPUT_TXT_DIR}' (Số liệu).")


if __name__ == "__main__":
    main()
