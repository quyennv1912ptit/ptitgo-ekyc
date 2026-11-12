import os
import shutil

from sklearn.model_selection import train_test_split

# ================= CẤU HÌNH =================
NEGATIVE_DIR = "./Sorted_Dataset/invalid_non_doc"
DATASET_DIR = "./card_dataset"
NUM_NEGATIVES = 200  # Tổng số ảnh rác muốn lấy
# ============================================


def process_negatives(img_list, split_name):
    img_dir = os.path.join(DATASET_DIR, "images", split_name)
    lbl_dir = os.path.join(DATASET_DIR, "labels", split_name)

    count = 0
    for img_name in img_list:
        base_name = os.path.splitext(img_name)[0]
        src_img = os.path.join(NEGATIVE_DIR, img_name)
        dst_img = os.path.join(img_dir, f"neg_{img_name}")
        dst_lbl = os.path.join(lbl_dir, f"neg_{base_name}.txt")

        # 1. Copy ảnh
        shutil.copy(src_img, dst_img)

        # 2. Tạo file nhãn TRỐNG
        with open(dst_lbl, "w", encoding="utf-8"):
            pass

        count += 1
    print(f" Đã bơm {count} ảnh âm vào tập {split_name.upper()}")


def main():
    if not os.path.exists(NEGATIVE_DIR):
        print(f"Không tìm thấy thư mục: {NEGATIVE_DIR}")
        return

    # Lấy danh sách ảnh âm
    images = [f for f in os.listdir(NEGATIVE_DIR) if f.lower().endswith((".png", ".jpg", ".jpeg"))]

    if len(images) < NUM_NEGATIVES:
        images_to_use = images
    else:
        images_to_use = images[:NUM_NEGATIVES]

    if not images_to_use:
        print("Không có ảnh âm nào để thêm.")
        return

    print(f"Đang xử lý tổng cộng {len(images_to_use)} ảnh âm...")

    # Chia tỷ lệ 70% Train, 15% Val, 15% Test
    train_imgs, temp_imgs = train_test_split(images_to_use, test_size=0.3, random_state=42)
    val_imgs, test_imgs = train_test_split(temp_imgs, test_size=0.5, random_state=42)

    # Đưa vào các thư mục tương ứng
    process_negatives(train_imgs, "train")
    process_negatives(val_imgs, "val")
    process_negatives(test_imgs, "test")

    print("\nHoàn tất! Hãy nén lại thư mục 'card_dataset' và đẩy lên Kaggle để train V2.")


if __name__ == "__main__":
    main()
