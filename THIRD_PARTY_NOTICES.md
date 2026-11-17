# Third-Party Notices and License Summary

This repository (`ekyc-service`) is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)** — see [LICENSE](LICENSE).

## 1. Copyleft Dependencies Driving Repository License

| Component | Version | License | Scope / Impact |
| --- | --- | --- | --- |
| `ultralytics-opencv-headless` (+ `ultralytics-platform`, `ultralytics-thop`) | `8.4.208` | **AGPL-3.0** | Core card-pose detector (`src/ekyc/pipeline/yolo_card_detector.py`) and YOLO training/evaluation scripts. Under AGPL-3.0 Section 13, anyone operating a modified network service must offer the complete Corresponding Source at no charge to remote network users, unless operating under a separate commercial/enterprise license from Ultralytics. |
| `Unidecode` | `1.4.0` | **GPL-2.0-or-later** | Vietnamese ASCII-folding / text normalization helpers. Requires GPL-compatible licensing for distributed/combined works. |

## 2. Permissive & Mixed-Permissive Runtime Dependencies

| Component | Version | License |
| --- | --- | --- |
| `fastapi` | `0.162.2` | MIT |
| `starlette` | `1.10.0` | BSD-3-Clause |
| `uvicorn` | `0.54.0` | BSD-3-Clause |
| `pydantic` / `pydantic-settings` | `2.14.3` / `2.16.2` | MIT |
| `httpx` | `0.28.1` | BSD-3-Clause |
| `numpy` | `2.4.6` | BSD-3-Clause / 0BSD / MIT / Zlib / CC0-1.0 |
| `opencv-python-headless` | `5.0.0.93` | Apache-2.0 |
| `pillow` | `13.0.0` | MIT-CMU |
| `rapidfuzz` | `3.14.6` | MIT |
| `cryptography` | `50.0.2` | Apache-2.0 OR BSD-3-Clause |
| `firebase-admin` | `7.8.0` | Apache-2.0 |
| `tzdata` | `2026.6` | Apache-2.0 |
| `torch` | `2.15.0` | Apache-2.0 / BSD-2-Clause / BSD-3-Clause / BSL-1.0 |
| `torchvision` | `0.29.2` | BSD-3-Clause |
| `easyocr` | `1.7.2` | Apache-2.0 |
| `psutil` | `7.2.2` | BSD-3-Clause |
| `PyYAML` | `6.0.3` | MIT |
| `albumentations` | `2.0.8` | MIT |
| `scikit-learn` | `1.9.1` | BSD-3-Clause |
| `huggingface-hub` | `2.4.0` | Apache-2.0 |
| `onnxruntime` | `1.31.1` | MIT |

## 3. Model Artifacts and External Datasets (Not Distributed in Git)

Model weights and training datasets are **not** included in this Git repository and are restored externally (e.g., via a read-only Modal Volume or local cache verified against SHA-256 checksums in `src/ekyc/services/model_artifacts.py`):

| Artifact | Origin | License / Distribution Status |
| --- | --- | --- |
| `craft_mlt_25k.pth` | EasyOCR / Clova AI CRAFT | MIT |
| `latin_g2.pth` | EasyOCR Latin G2 recognizer | Apache-2.0 |
| `face_detection_yunet_2023mar.onnx` *(local experimental only)* | OpenCV Zoo (YuNet) | MIT |
| `face_recognition_sface_2021dec.onnx` *(local experimental only)* | OpenCV Zoo (SFace) | Apache-2.0 |
| `card_pose_v3_best.pt` | Custom fine-tuned YOLO Pose checkpoint | Subject to **AGPL-3.0** (Ultralytics) and training dataset terms; **not** distributed in Git. |
| Referenced training datasets (`hr16/Raw-Vietnamese-ID`, Roboflow Vietnamese ID Card) | External third-party repositories | Subject to upstream dataset terms (`CC BY 4.0` on Roboflow where noted); **not** redistributed here. |

## 4. Scope Boundary Across Separate Repositories

The AGPL-3.0 license and these notices apply to **`ekyc-service`** as an independent service communicating over HTTP/JSON boundaries. Separate client/backend repositories in the broader PtitGo ecosystem maintain their own independent repositories and license choices.
