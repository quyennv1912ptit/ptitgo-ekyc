import sys
from types import SimpleNamespace

import numpy as np
import pytest

from ekyc.config import Settings
from ekyc.pipeline.card_image import DefaultCardPreparer
from ekyc.pipeline.ocr.easyocr_engine import EasyOcrEngine


def test_engine_without_model_download(monkeypatch: pytest.MonkeyPatch) -> None:
    class Reader:
        def __init__(self, languages: list[str], **kwargs: object) -> None:
            assert languages == ["vi", "en"]
            self.calls = 0

        def readtext(self, image: np.ndarray, *, detail: int, paragraph: bool) -> list:
            assert detail == 1 and paragraph is False
            self.calls += 1
            return [
                ([[20, 10], [0, 12], [1, 30], [22, 28]], " TEST ", 0.8),
                ([[0, 0]] * 4, " ", 0.2),
            ]

    monkeypatch.setitem(sys.modules, "easyocr", SimpleNamespace(Reader=Reader))
    engine = EasyOcrEngine(["vi", "en"])
    result = engine.read_lines(np.zeros((32, 64, 3), dtype=np.uint8))
    assert len(result) == 1 and result[0].text == "TEST"
    assert (result[0].x1, result[0].y1, result[0].x2, result[0].y2) == (0, 10, 22, 30)
    engine.warm_up()
    assert engine.reader.calls == 2


def test_card_preparer(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    image = np.zeros((50, 80, 3), dtype=np.uint8)
    calls = []
    config = settings.model_copy(update={"quality_profile": "calibration"})

    def load(url: str, settings_arg: Settings) -> object:
        calls.append("load")
        assert settings_arg is config
        return image

    def quality(loaded: object, config: Settings) -> None:
        calls.append("quality")
        assert loaded is image

    def warp(loaded: object) -> object:
        calls.append("warp")
        return SimpleNamespace(warped_image=image)

    monkeypatch.setattr("ekyc.pipeline.image_loader.load_image_from_url_sync", load)
    monkeypatch.setattr("ekyc.pipeline.quality.check_warped_quality", quality)
    monkeypatch.setattr("ekyc.pipeline.card_detector.detect_and_warp_card", warp)
    prepared = DefaultCardPreparer(config).prepare("unused")
    assert prepared.image.shape == image.shape
    assert calls == ["load", "warp", "quality"]


def test_app_easyocr_lifespan(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from ekyc.main import create_app

    calls = []

    class FakeWorker:
        def __init__(self, settings: Settings) -> None:
            assert settings.ocr_backend == "easyocr"
            calls.append("init")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr("ekyc.main.CccdWorker", FakeWorker)
    with TestClient(create_app(settings.model_copy(update={"ocr_backend": "easyocr"}))) as client:
        assert client.get("/health").status_code == 200
        assert isinstance(client.app.state.cccd_worker, FakeWorker)
    assert calls == ["init", "close"]


@pytest.mark.parametrize("width", [None, 0.2])
def test_width_threshold(monkeypatch: pytest.MonkeyPatch, width: float | None) -> None:
    calls = []

    class Reader:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def readtext(self, image: np.ndarray, **kwargs: object) -> list:
            calls.append(kwargs)
            return []

    monkeypatch.setitem(sys.modules, "easyocr", SimpleNamespace(Reader=Reader))
    EasyOcrEngine(["vi", "en"], width_ths=width).read_lines(np.zeros((10, 10, 3), dtype=np.uint8))
    assert calls == [
        {"detail": 1, "paragraph": False, **({"width_ths": width} if width is not None else {})}
    ]
