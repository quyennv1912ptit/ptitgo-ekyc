import os
import cv2
import shutil

# Cấu hình đường dẫn
SOURCE_DIR = "./Raw-Vietnamese-ID/results/results" # Thư mục đang chứa ảnh trộn lẫn
OUTPUT_DIR = "./Sorted_Dataset"          # Thư mục đích
CATEGORIES = {
    ord('1'): "cccd_front",
    ord('2'): "cmnd_front",
    ord('3'): "gplx_front",
    ord('4'): "invalid_non_doc",
    ord('5'): "invalid_unsupported",
    ord('6'): "review_needed"
}
def main():
    # Khởi tạo các thư mục đích nếu chưa có
    for cat in CATEGORIES.values():
        os.makedirs(os.path.join(OUTPUT_DIR, cat), exist_ok=True)

    # Lấy danh sách các ảnh còn lại trong queue
    valid_exts = ('.png', '.jpg', '.jpeg')
    images = [f for f in os.listdir(SOURCE_DIR) if f.lower().endswith(valid_exts)]
    total_images = len(images)
    
    if total_images == 0:
        print("Tuyệt vời! Không còn ảnh nào cần phân loại.")
        return

    print(f"Tìm thấy {total_images} ảnh cần xử lý.")
    print("-" * 35)
    print("HƯỚNG DẪN PHÍM BẤM:")
    for key, folder in CATEGORIES.items():
        print(f" Phím {chr(key)} -> {folder}")
    print("\n Phím SPACE -> Bỏ qua (ảnh vẫn nằm ở thư mục gốc)")
    print(" Phím 'q' hoặc ESC -> Dừng tool (lưu trạng thái)")
    print("-" * 35)

    # Dùng enumerate để đếm tiến độ (bắt đầu từ 1)
    for idx, img_name in enumerate(images, start=1):
        img_path = os.path.join(SOURCE_DIR, img_name)
        img = cv2.imread(img_path)
        
        if img is None:
            print(f"Lỗi đọc ảnh: {img_name}")
            continue

        # Resize ảnh để hiển thị vừa màn hình PC
        height, width = img.shape[:2]
        max_size = 800
        if max(height, width) > max_size:
            scale = max_size / max(height, width)
            img = cv2.resize(img, (int(width * scale), int(height * scale)))

        # Vẽ text tiến độ lên góc trái ảnh (màu xanh lá)
        progress_text = f"Tien do: {idx} / {total_images}"
        cv2.putText(img, progress_text, (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 
                    1, (0, 255, 0), 2, cv2.LINE_AA)

        while True:
            cv2.imshow("Labeling Tool (Focus vao cua so nay)", img)
            
            # Đợi người dùng bấm phím
            key = cv2.waitKey(0)

            # Thoát script (q hoặc ESC)
            if key in [ord('q'), 27]: 
                print(f"\nĐã dừng ở ảnh thứ {idx}. Lần sau chạy sẽ quét lại số ảnh còn sót.")
                cv2.destroyAllWindows()
                return
            
            # Skip ảnh này
            elif key == 32: # SPACE
                print(f"[{idx}/{total_images}] Bỏ qua {img_name}")
                break

            # Phân loại và di chuyển file
            elif key in CATEGORIES:
                category = CATEGORIES[key]
                dest_path = os.path.join(OUTPUT_DIR, category, img_name)
                
                shutil.move(img_path, dest_path)
                print(f"[{idx}/{total_images}] Đã chuyển: {img_name} -> {category}")
                break
            
            else:
                print("Phím không hợp lệ. Vui lòng bấm 1-8, Space để skip, hoặc q để thoát.")

    print("\nĐã phân loại xong toàn bộ dataset!")
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()