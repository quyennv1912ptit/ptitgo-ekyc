from threading import Lock

import numpy as np

from ekyc.pipeline.ocr.base import OcrLine


class EasyOcrEngine:
    def __init__(
        self, languages: list[str], *, gpu: bool = False, width_ths: float | None = None
    ) -> None:
        import easyocr

        self.reader = easyocr.Reader(languages, gpu=gpu)
        self._lock = Lock()
        self.width_ths = width_ths

    def read_lines(self, image: np.ndarray) -> list[OcrLine]:
        with self._lock:
            options = {} if self.width_ths is None else {"width_ths": self.width_ths}
            boxes = self.reader.readtext(image, detail=1, paragraph=False, **options)
        result = []
        for points, text, confidence in boxes:
            text = text.strip()
            if not text:
                continue
            xs, ys = zip(*points, strict=True)
            result.append(
                OcrLine(
                    text,
                    float(confidence),
                    float(min(xs)),
                    float(min(ys)),
                    float(max(xs)),
                    float(max(ys)),
                )
            )
        return result

    def warm_up(self) -> None:
        self.read_lines(np.full((64, 128, 3), 255, dtype=np.uint8))
