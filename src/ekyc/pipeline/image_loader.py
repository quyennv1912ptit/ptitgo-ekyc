import io
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import cv2
import httpx
import numpy as np
from PIL import Image, ImageOps

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LoadedImage:
    """Container for decoded image data in BGR and RGB formats."""

    bgr: np.ndarray
    rgb: np.ndarray
    width: int
    height: int
    raw_bytes: bytes
    url: str | None = None

    @property
    def image(self) -> np.ndarray:
        """Default image representation (OpenCV BGR ndarray)."""
        return self.bgr

    @property
    def channels(self) -> int:
        return 3

    @property
    def shape(self) -> tuple[int, int, int]:
        return self.bgr.shape


def is_host_allowed(hostname: str, allowed_hosts: list[str]) -> bool:
    """Check if the given hostname matches or is a subdomain of any allowed host."""
    normalized_hostname = hostname.lower().strip()
    for allowed in allowed_hosts:
        normalized_allowed = allowed.lower().strip()
        if not normalized_allowed:
            continue
        if normalized_hostname == normalized_allowed or normalized_hostname.endswith(
            f".{normalized_allowed}"
        ):
            return True
    return False


def validate_image_url(url: str, allowed_hosts: list[str]) -> None:
    """Validate that the image URL has http/https scheme and its host is whitelisted."""
    if not url or not isinstance(url, str):
        raise EkycError(ErrorCode.URL_NOT_ALLOWED)

    try:
        parsed = urlsplit(url.strip())
    except Exception as exc:
        raise EkycError(ErrorCode.URL_NOT_ALLOWED) from exc

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise EkycError(ErrorCode.URL_NOT_ALLOWED)

    if not is_host_allowed(parsed.hostname, allowed_hosts):
        raise EkycError(ErrorCode.URL_NOT_ALLOWED)


async def fetch_image_bytes(
    url: str,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> bytes:
    """Fetch raw image bytes from URL while enforcing whitelist and byte size limit."""
    validate_image_url(url, settings.allowed_image_hosts)

    async def _fetch(c: httpx.AsyncClient) -> bytes:
        try:
            async with c.stream(
                "GET", url, timeout=settings.fetch_timeout_s, follow_redirects=True
            ) as response:
                if response.status_code != 200:
                    raise EkycError(ErrorCode.IMAGE_FETCH_FAILED)

                # Bảo vệ chống SSRF chuyển hướng sang host ngoài danh sách cho phép
                if response.url.host and not is_host_allowed(
                    response.url.host, settings.allowed_image_hosts
                ):
                    raise EkycError(ErrorCode.URL_NOT_ALLOWED)

                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit():
                    if int(content_length) > settings.max_image_bytes:
                        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)

                chunks = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    chunks.extend(chunk)
                    if len(chunks) > settings.max_image_bytes:
                        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)

                if not chunks:
                    raise EkycError(ErrorCode.INVALID_IMAGE)

                return bytes(chunks)
        except EkycError:
            raise
        except (httpx.TimeoutException, httpx.HTTPError, httpx.RequestError) as exc:
            # Không log URL để tránh làm lộ chữ ký hoặc thông tin định danh
            logger.warning("image_fetch_failed: %s", exc.__class__.__name__)
            raise EkycError(ErrorCode.IMAGE_FETCH_FAILED) from exc

    if client is not None:
        return await _fetch(client)

    async with httpx.AsyncClient(timeout=settings.fetch_timeout_s) as new_client:
        return await _fetch(new_client)


def fetch_image_bytes_sync(
    url: str,
    settings: Settings,
    client: httpx.Client | None = None,
) -> bytes:
    """Fetch raw image bytes synchronously while enforcing whitelist and byte size limit."""
    validate_image_url(url, settings.allowed_image_hosts)

    def _fetch(c: httpx.Client) -> bytes:
        try:
            with c.stream(
                "GET", url, timeout=settings.fetch_timeout_s, follow_redirects=True
            ) as response:
                if response.status_code != 200:
                    raise EkycError(ErrorCode.IMAGE_FETCH_FAILED)

                if response.url.host and not is_host_allowed(
                    response.url.host, settings.allowed_image_hosts
                ):
                    raise EkycError(ErrorCode.URL_NOT_ALLOWED)

                content_length = response.headers.get("content-length")
                if content_length and content_length.isdigit():
                    if int(content_length) > settings.max_image_bytes:
                        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)

                chunks = bytearray()
                for chunk in response.iter_bytes(chunk_size=65536):
                    chunks.extend(chunk)
                    if len(chunks) > settings.max_image_bytes:
                        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)

                if not chunks:
                    raise EkycError(ErrorCode.INVALID_IMAGE)

                return bytes(chunks)
        except EkycError:
            raise
        except (httpx.TimeoutException, httpx.HTTPError, httpx.RequestError) as exc:
            logger.warning("image_fetch_failed: %s", exc.__class__.__name__)
            raise EkycError(ErrorCode.IMAGE_FETCH_FAILED) from exc

    if client is not None:
        return _fetch(client)

    with httpx.Client(timeout=settings.fetch_timeout_s) as new_client:
        return _fetch(new_client)


def decode_and_transpose_image(
    raw_bytes: bytes,
    max_pixels: int,
    url: str | None = None,
) -> LoadedImage:
    """Decode raw bytes, validate pixel count, auto-rotate EXIF and convert to RGB/BGR."""
    if not raw_bytes:
        raise EkycError(ErrorCode.INVALID_IMAGE)

    Image.MAX_IMAGE_PIXELS = max_pixels

    try:
        pil_img = Image.open(io.BytesIO(raw_bytes))
        pil_img.load()
    except Image.DecompressionBombError as exc:
        raise EkycError(ErrorCode.IMAGE_TOO_LARGE) from exc
    except Exception as exc:
        logger.warning("invalid_image_decode_failed: %s", exc.__class__.__name__)
        raise EkycError(ErrorCode.INVALID_IMAGE) from exc

    width, height = pil_img.size
    if width * height > max_pixels:
        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)

    # Tự động xoay ảnh theo cờ EXIF Orientation (Orientation 1-8)
    try:
        transposed = ImageOps.exif_transpose(pil_img)
        if transposed is not None:
            pil_img = transposed
    except Exception as exc:
        logger.debug("exif_transpose_skipped: %s", exc.__class__.__name__)

    # Chuẩn hóa về không gian màu RGB
    if pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")

    width, height = pil_img.size
    rgb = np.array(pil_img, dtype=np.uint8)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

    return LoadedImage(
        bgr=bgr,
        rgb=rgb,
        width=width,
        height=height,
        raw_bytes=raw_bytes,
        url=url,
    )


async def load_image_from_url(
    url: str,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> LoadedImage:
    """Download image from whitelisted URL, enforce limits, rotate EXIF and decode (async)."""
    raw_bytes = await fetch_image_bytes(url, settings, client=client)
    return decode_and_transpose_image(raw_bytes, settings.max_image_pixels, url=url)


def load_image_from_url_sync(
    url: str,
    settings: Settings,
    client: httpx.Client | None = None,
) -> LoadedImage:
    """Download image from whitelisted URL, enforce limits, rotate EXIF and decode (sync)."""
    raw_bytes = fetch_image_bytes_sync(url, settings, client=client)
    return decode_and_transpose_image(raw_bytes, settings.max_image_pixels, url=url)


def load_image_from_bytes(
    raw_bytes: bytes,
    settings: Settings,
) -> LoadedImage:
    """Decode, enforce byte and pixel limits, and auto-rotate EXIF from raw bytes."""
    if len(raw_bytes) > settings.max_image_bytes:
        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)
    return decode_and_transpose_image(raw_bytes, settings.max_image_pixels)
