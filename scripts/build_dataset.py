import os
import json
import cv2
import numpy as np
import shutil
import albumentations as A
from collections import defaultdict
from sklearn.model_selection import train_test_split

INPUT_DIR = "./Sorted_Dataset/train"
OUTPUT_DIR = "./card_dataset"
NUM_AUGMENTATIONS = 10

def setup_directories():
    if os.path.exists(OUTPUT_DIR):
        shutil.rmtree(OUTPUT_DIR)
    
    for split in ['train', 'val', 'test']:
        os.makedirs(os.path.join(OUTPUT_DIR, 'images', split), exist_ok=True)
        os.makedirs(os.path.join(OUTPUT_DIR, 'labels', split), exist_ok=True)

def order_points_clockwise(pts):
    rect = np.zeros((4, 2), dtype="float32")
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)] # Trái-Trên có tổng x+y nhỏ nhất
    rect[2] = pts[np.argmax(s)] # Phải-Dưới có tổng x+y lớn nhất
    
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)] # Phải-Trên có x-y nhỏ nhất (y nhỏ x lớn)
    rect[3] = pts[np.argmax(diff)] # Trái-Dưới có x-y lớn nhất (y lớn x nhỏ)
    return rect

def convert_to_yolo(points, img_w, img_h):
    pts = np.array(points)
    x_min, x_max = pts[:, 0].min(), pts[:, 0].max()
    y_min, y_max = pts[:, 1].min(), pts[:, 1].max()

    # Thêm padding nhẹ cho Bounding Box
    pad = 5
    x_min, y_min = max(0, x_min - pad), max(0, y_min - pad)
    x_max, y_max = min(img_w, x_max + pad), min(img_h, y_max + pad)

    # Tính Tâm, Rộng, Cao
    box_w, box_h = x_max - x_min, y_max - y_min
    cx, cy = x_min + box_w / 2, y_min + box_h / 2

    # Chuẩn hóa về [0, 1]
    cx, cy = cx / img_w, cy / img_h
    bw, bh = box_w / img_w, box_h / img_h

    # Format YOLO: class cx cy bw bh x1 y1 2 x2 y2 2...
    line = f"0 {cx:.6f} {cy:.6f} {bw:.6f} {bh:.6f}"
    for pt in pts:
        px, py = pt[0] / img_w, pt[1] / img_h
        # Clamp tọa độ trong khoảng [0, 1] để tránh lỗi báo ngoài ảnh
        px, py = max(0.0, min(1.0, px)), max(0.0, min(1.0, py))
        line += f" {px:.6f} {py:.6f} 2"
    return line

def get_augmentation_pipeline():
    """Pipeline biến đổi ảnh và tọa độ tự động"""
    return A.Compose([
        A.Affine(rotate=(-30, 30), scale=(0.7, 1.1), shear=(-10, 10), p=0.8), # Xoay, thu phóng, nghiêng
        A.Perspective(scale=(0.02, 0.08), p=0.5),                            # Phối cảnh 3D
        A.RandomBrightnessContrast(brightness_limit=0.2, contrast_limit=0.2, p=0.5), # Đổi sáng/tối
        A.MotionBlur(blur_limit=5, p=0.2),                                   # Chuyển động mờ
        A.GaussNoise(var_limit=(10.0, 50.0), p=0.3),                         # Thêm nhiễu hạt
    ], keypoint_params=A.KeypointParams(format='xy', remove_invisible=False))

def create_yaml():
    """Tạo file data.yaml cho mô hình huấn luyện"""
    yaml_content = f"""path: {os.path.abspath(OUTPUT_DIR)}
train: images/train
val: images/val
test: images/test

# Số điểm Keypoints (4 điểm, 3 giá trị x,y,visible)
kpt_shape: [4, 3]

names:
  0: card
"""
    with open(os.path.join(OUTPUT_DIR, 'data.yaml'), 'w', encoding='utf-8') as f:
        f.write(yaml_content)

def main():
    setup_directories()
    augmentor = get_augmentation_pipeline()

    # 1. Gom nhóm ảnh theo Card ID (Giả sử file tên c01_front_1.jpg -> ID là c01)
    card_groups = defaultdict(list)
    for file in os.listdir(INPUT_DIR):
        if file.lower().endswith(('.png', '.jpg', '.jpeg')):
            card_id = file.split('_')[0] 
            card_groups[card_id].append(file)
            
    card_ids = list(card_groups.keys())
    print(f"Phát hiện {len(card_ids)} chiếc thẻ vật lý khác nhau.")

    # 2. Chia tập Train (70%), Val (15%), Test (15%) theo ID thẻ
    train_ids, temp_ids = train_test_split(card_ids, test_size=0.3, random_state=42)
    val_ids, test_ids = train_test_split(temp_ids, test_size=0.5, random_state=42)

    def process_split(ids, split_name, augment=False):
        count = 0
        for cid in ids:
            for img_name in card_groups[cid]:
                base_name = os.path.splitext(img_name)[0]
                img_path = os.path.join(INPUT_DIR, img_name)
                json_path = os.path.join(INPUT_DIR, base_name + '.json')

                if not os.path.exists(json_path):
                    continue

                # Đọc dữ liệu
                img = cv2.imread(img_path)
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # Trích xuất 4 điểm góc (nếu dùng SAM có thể sinh ra >4 điểm, ta dùng approxPolyDP để lấy 4)
                raw_points = np.array(data['shapes'][0]['points'], dtype=np.float32)
                if len(raw_points) > 4:
                    rect = cv2.minAreaRect(raw_points)
                    raw_points = cv2.boxPoints(rect)
                
                ordered_points = order_points_clockwise(raw_points)
                img_h, img_w = img.shape[:2]

                # Lưu ảnh GỐC vào dataset
                out_img_path = os.path.join(OUTPUT_DIR, 'images', split_name, img_name)
                out_lbl_path = os.path.join(OUTPUT_DIR, 'labels', split_name, base_name + '.txt')
                
                cv2.imwrite(out_img_path, img)
                with open(out_lbl_path, 'w') as f:
                    f.write(convert_to_yolo(ordered_points, img_w, img_h))
                count += 1

                # 3. Sinh Dữ liệu Tổng hợp (CHỈ DÀNH CHO TẬP TRAIN)
                if augment:
                    for i in range(NUM_AUGMENTATIONS):
                        aug_img_name = f"aug_{i}_{img_name}"
                        aug_lbl_name = f"aug_{i}_{base_name}.txt"
                        
                        # Albumentations tự động xoay cả ảnh và tọa độ điểm
                        transformed = augmentor(image=img, keypoints=ordered_points.tolist())
                        t_img = transformed['image']
                        t_pts = transformed['keypoints']
                        
                        # Chỉ lưu nếu nó vẫn giữ đủ 4 điểm trong khung hình
                        if len(t_pts) == 4:
                            t_pts = order_points_clockwise(np.array(t_pts))
                            t_h, t_w = t_img.shape[:2]
                            
                            cv2.imwrite(os.path.join(OUTPUT_DIR, 'images', split_name, aug_img_name), t_img)
                            with open(os.path.join(OUTPUT_DIR, 'labels', split_name, aug_lbl_name), 'w') as f:
                                f.write(convert_to_yolo(t_pts, t_w, t_h))
                            count += 1
                            
        print(f"Đã xử lý tập {split_name.upper()}: Sinh ra {count} ảnh.")

    # 4. Thực thi
    process_split(train_ids, 'train', augment=True)
    process_split(val_ids, 'val', augment=False)
    process_split(test_ids, 'test', augment=False)
    create_yaml()
    print("\nHoàn tất! Dataset đã sẵn sàng tại thư mục:", OUTPUT_DIR)

if __name__ == "__main__":
    main()