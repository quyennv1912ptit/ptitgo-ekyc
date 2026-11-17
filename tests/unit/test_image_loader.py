import io
import socket

import httpx
import numpy as np
import pytest
from PIL import Image

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.image_loader import (
    decode_and_transpose_image,
    fetch_image_bytes_sync,
    is_host_allowed,
    load_image_from_bytes,
    load_image_from_url_sync,
    validate_image_url,
)


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kw: dns_answer("8.8.8.8"))


def dns_answer(address):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        ekyc_api_key="test-key",
        allowed_image_hosts=["res.cloudinary.com", "storage.googleapis.com"],
        max_image_bytes=1000,
        max_image_pixels=10000,
        fetch_timeout_s=2.0,
    )


def create_test_image(width: int = 20, height: int = 10, orientation: int | None = None) -> bytes:
    """Generate a simple test JPEG image with optional EXIF orientation."""
    img = Image.new("RGB", (width, height), color=(255, 0, 0))
    buffer = io.BytesIO()
    if orientation is not None:
        exif = img.getexif()
        exif[0x0112] = orientation
        img.save(buffer, format="JPEG", exif=exif)
    else:
        img.save(buffer, format="JPEG")
    return buffer.getvalue()


def test_is_host_allowed() -> None:
    allowed = ["res.cloudinary.com", "example.com"]
    assert is_host_allowed("res.cloudinary.com", allowed)
    assert is_host_allowed("sub.res.cloudinary.com", allowed)
    assert is_host_allowed("example.com", allowed)
    assert not is_host_allowed("cloudinary.com", allowed)
    assert not is_host_allowed("evil-res.cloudinary.com.fake", allowed)
    assert not is_host_allowed("google.com", allowed)


def test_validate_image_url(settings: Settings) -> None:
    # Valid URLs
    validate_image_url(
        "https://res.cloudinary.com/demo/image/upload/sample.jpg", settings.allowed_image_hosts
    )
    validate_image_url(
        "http://storage.googleapis.com/bucket/image.png", settings.allowed_image_hosts
    )

    # Disallowed host
    with pytest.raises(EkycError) as exc_info:
        validate_image_url("https://malicious.com/image.jpg", settings.allowed_image_hosts)
    assert exc_info.value.code == ErrorCode.URL_NOT_ALLOWED

    # Disallowed scheme
    with pytest.raises(EkycError) as exc_info:
        validate_image_url("ftp://res.cloudinary.com/image.jpg", settings.allowed_image_hosts)
    assert exc_info.value.code == ErrorCode.URL_NOT_ALLOWED

    # Empty / malformed
    with pytest.raises(EkycError) as exc_info:
        validate_image_url("", settings.allowed_image_hosts)
    assert exc_info.value.code == ErrorCode.URL_NOT_ALLOWED


def test_fetch_image_bytes_success(settings: Settings) -> None:
    img_bytes = create_test_image(10, 10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=img_bytes)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_image_bytes_sync(
            "https://res.cloudinary.com/demo/test.jpg", settings, client=client
        )
    assert result == img_bytes


def test_fetch_image_bytes_too_large_header(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-length": "2000"}  # max_image_bytes is 1000
        return httpx.Response(200, headers=headers, content=b"a" * 10)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EkycError) as exc_info:
            fetch_image_bytes_sync(
                "https://res.cloudinary.com/demo/large.jpg", settings, client=client
            )
    assert exc_info.value.code == ErrorCode.IMAGE_TOO_LARGE


def test_fetch_image_bytes_too_large_stream(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EkycError) as exc_info:
            fetch_image_bytes_sync(
                "https://res.cloudinary.com/demo/large.jpg", settings, client=client
            )
    assert exc_info.value.code == ErrorCode.IMAGE_TOO_LARGE


def test_fetch_image_bytes_http_error(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EkycError) as exc_info:
            fetch_image_bytes_sync(
                "https://res.cloudinary.com/demo/notfound.jpg", settings, client=client
            )
    assert exc_info.value.code == ErrorCode.IMAGE_FETCH_FAILED


def test_fetch_image_bytes_empty(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(EkycError) as exc_info:
            fetch_image_bytes_sync(
                "https://res.cloudinary.com/demo/empty.jpg", settings, client=client
            )
    assert exc_info.value.code == ErrorCode.INVALID_IMAGE


def test_decode_and_transpose_image(settings: Settings) -> None:
    # Test normal image
    img_bytes = create_test_image(width=30, height=20)
    loaded = decode_and_transpose_image(img_bytes, settings.max_image_pixels)
    assert loaded.width == 30
    assert loaded.height == 20
    assert loaded.bgr.shape == (20, 30, 3)
    assert loaded.rgb.shape == (20, 30, 3)
    assert isinstance(loaded.image, np.ndarray)

    # Test corrupted image
    with pytest.raises(EkycError) as exc_info:
        decode_and_transpose_image(b"not an image", settings.max_image_pixels)
    assert exc_info.value.code == ErrorCode.INVALID_IMAGE

    # Test pixel limit
    with pytest.raises(EkycError) as exc_info:
        # max_image_pixels is 10000, 200x100 = 20000
        large_bytes = create_test_image(width=200, height=100)
        decode_and_transpose_image(large_bytes, settings.max_image_pixels)
    assert exc_info.value.code == ErrorCode.IMAGE_TOO_LARGE


def test_exif_rotation(settings: Settings) -> None:
    # Orientation 6 means rotated 90 CW (so original 40x20 becomes 20x40 after transpose)
    img_bytes = create_test_image(width=40, height=20, orientation=6)
    loaded = decode_and_transpose_image(img_bytes, settings.max_image_pixels)
    assert loaded.width == 20
    assert loaded.height == 40
    assert loaded.bgr.shape == (40, 20, 3)


def test_load_image_from_url(settings: Settings) -> None:
    img_bytes = create_test_image(width=25, height=15)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=img_bytes)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        loaded = load_image_from_url_sync(
            "https://res.cloudinary.com/test/photo.jpg", settings, client=client
        )
    assert loaded.width == 25
    assert loaded.height == 15
    assert loaded.url == "https://res.cloudinary.com/test/photo.jpg"


def test_load_image_from_bytes(settings: Settings) -> None:
    img_bytes = create_test_image(width=15, height=10)
    loaded = load_image_from_bytes(img_bytes, settings)
    assert loaded.width == 15
    assert loaded.height == 10

    # Over byte limit
    with pytest.raises(EkycError) as exc_info:
        load_image_from_bytes(b"x" * 2000, settings)
    assert exc_info.value.code == ErrorCode.IMAGE_TOO_LARGE


@pytest.mark.parametrize("location", ["https://outside.example/image", "http://127.0.0.1/photo"])
def test_redirect_never_contacted(settings, location):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": location})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(EkycError) as info:
            fetch_image_bytes_sync("https://res.cloudinary.com/photo", settings, client)
    assert info.value.code == ErrorCode.URL_NOT_ALLOWED
    assert len(calls) == 1
    assert calls[0].url.host == "8.8.8.8"
    assert calls[0].headers["host"] == "res.cloudinary.com"
    assert calls[0].extensions["sni_hostname"] == "res.cloudinary.com"


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "10.0.0.1", "::1", "fd00::1"])
def test_rejects_mixed_dns_answers_before_transport(settings, monkeypatch, address):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kw: dns_answer("8.8.8.8") + dns_answer(address)
    )
    transport = httpx.MockTransport(lambda request: pytest.fail("network call"))
    with httpx.Client(transport=transport) as c:
        with pytest.raises(EkycError) as info:
            fetch_image_bytes_sync("https://res.cloudinary.com/photo", settings, c)
    assert info.value.code == ErrorCode.URL_NOT_ALLOWED


def test_pixel_limit_before_decode(settings, monkeypatch):
    data = create_test_image(200, 100)
    monkeypatch.setattr("PIL.JpegImagePlugin.JpegImageFile.load", lambda *a: pytest.fail("decoded"))
    with pytest.raises(EkycError) as info:
        load_image_from_bytes(data, settings)
    assert info.value.code == ErrorCode.IMAGE_TOO_LARGE


def test_fake_extension(settings):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "image/jpeg"}, content=b"text"
            )
        )
    ) as client:
        with pytest.raises(EkycError) as info:
            load_image_from_url_sync("https://res.cloudinary.com/fake.jpg", settings, client)
    assert info.value.code == ErrorCode.INVALID_IMAGE


def test_animated_png_rejected(settings):
    buffer = io.BytesIO()
    frame = Image.new("RGB", (8, 8))
    frame.save(buffer, format="PNG", save_all=True, append_images=[Image.new("RGB", (8, 8), "red")])
    with pytest.raises(EkycError) as info:
        load_image_from_bytes(buffer.getvalue(), settings)
    assert info.value.code == ErrorCode.INVALID_IMAGE
