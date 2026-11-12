from pathlib import Path

from ekyc.config import Settings
from ekyc.core.logging import setup_logging


def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level)
    import easyocr

    reader = easyocr.Reader(settings.ocr_languages, gpu=False)
    print(f"Model directory: {Path(reader.model_storage_directory).resolve()}")


if __name__ == "__main__":
    main()
