import asyncio
import io
import ipaddress
import re
import socket
import warnings
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urlsplit

import cv2
import httpx
import numpy as np
from PIL import Image, ImageOps

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.core.inference_metrics import stage


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
    try:
        if not isinstance(url, str) or not url or re.search(r"[\s\\\x00-\x1f\x7f]", url):
            raise ValueError
        parsed = urlsplit(url)
        port = {"http": 80, "https": 443}[parsed.scheme]
        host = parsed.hostname
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.port not in (None, port)
            or parsed.netloc.lower() not in (host, f"{host}:{port}")
            or len(host) > 253
            or "." not in host
            or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            )
            or not host.rsplit(".", 1)[1][0].isalpha()
            or not is_host_allowed(host, allowed_hosts)
        ):
            raise ValueError
    except (ValueError, KeyError):
        raise EkycError(ErrorCode.URL_NOT_ALLOWED) from None


def pinned_request(url: str, settings: Settings) -> tuple[httpx.URL, dict, dict]:
    validate_image_url(url, settings.allowed_image_hosts)
    original = httpx.URL(url)
    try:
        records = socket.getaddrinfo(
            original.host,
            original.port or (443 if original.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    except (OSError, ValueError):
        raise EkycError(ErrorCode.IMAGE_FETCH_FAILED) from None
    if not addresses or any(
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or ip == ipaddress.ip_address("168.63.129.16")
        or getattr(ip, "scope_id", None) is not None
        or (
            isinstance(ip, ipaddress.IPv6Address)
            and (
                ip.ipv4_mapped
                or ip.sixtofour
                or ip.teredo
                or ip in ipaddress.ip_network("64:ff9b::/96")
            )
        )
        for ip in addresses
    ):
        raise EkycError(ErrorCode.URL_NOT_ALLOWED)
    # Connect to the checked address. Keep the original virtual host and TLS identity.
    return (
        original.copy_with(host=str(addresses[0])),
        {"Host": original.host, "Accept-Encoding": "identity", "Connection": "close"},
        {"sni_hostname": original.host},
    )


def validate_response(response: httpx.Response, limit: int) -> None:
    if 300 <= response.status_code < 400:
        raise EkycError(ErrorCode.URL_NOT_ALLOWED)
    if (
        response.status_code != 200
        or response.headers.get("content-encoding", "identity").lower() != "identity"
    ):
        raise EkycError(ErrorCode.IMAGE_FETCH_FAILED)
    length = response.headers.get("content-length")
    if length is not None and (not length.isascii() or not length.isdecimal()):
        raise EkycError(ErrorCode.IMAGE_FETCH_FAILED)
    if length is not None and len(length.lstrip("0")) > len(str(limit)):
        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)
    if length is not None and int(length.lstrip("0") or "0") > limit:
        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)


def append_chunk(buffer: bytearray, chunk: bytes, limit: int) -> None:
    if len(buffer) + len(chunk) > limit:
        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)
    buffer.extend(chunk)


async def fetch_image_bytes(
    url: str, settings: Settings, client: httpx.AsyncClient | None = None
) -> bytes:
    async def fetch(c: httpx.AsyncClient) -> bytes:
        target, headers, extensions = await asyncio.to_thread(pinned_request, url, settings)
        async with c.stream(
            "GET",
            target,
            headers=headers,
            extensions=extensions,
            timeout=settings.fetch_timeout_s,
            follow_redirects=False,
        ) as response:
            validate_response(response, settings.max_image_bytes)
            buffer = bytearray()
            async for chunk in response.aiter_bytes():
                append_chunk(buffer, chunk, settings.max_image_bytes)
            if not buffer:
                raise EkycError(ErrorCode.INVALID_IMAGE)
            return bytes(buffer)

    try:
        async with asyncio.timeout(settings.fetch_timeout_s):
            if client is not None:
                return await fetch(client)
            async with httpx.AsyncClient(trust_env=False) as new_client:
                return await fetch(new_client)
    except (TimeoutError, httpx.HTTPError):
        raise EkycError(ErrorCode.IMAGE_FETCH_FAILED) from None


def fetch_image_bytes_sync(
    url: str, settings: Settings, client: httpx.Client | None = None
) -> bytes:
    deadline = monotonic() + settings.fetch_timeout_s

    def remaining() -> float:
        budget = deadline - monotonic()
        if budget <= 0:
            raise EkycError(ErrorCode.IMAGE_FETCH_FAILED)
        return budget

    def fetch(c: httpx.Client) -> bytes:
        target, headers, extensions = pinned_request(url, settings)
        with c.stream(
            "GET",
            target,
            headers=headers,
            extensions=extensions,
            timeout=remaining(),
            follow_redirects=False,
        ) as response:
            remaining()
            validate_response(response, settings.max_image_bytes)
            buffer = bytearray()
            for chunk in response.iter_bytes():
                remaining()
                append_chunk(buffer, chunk, settings.max_image_bytes)
            remaining()
            if not buffer:
                raise EkycError(ErrorCode.INVALID_IMAGE)
            return bytes(buffer)

    try:
        if client is not None:
            return fetch(client)
        with httpx.Client(trust_env=False) as new_client:
            return fetch(new_client)
    except httpx.HTTPError:
        raise EkycError(ErrorCode.IMAGE_FETCH_FAILED) from None


def open_checked_image(
    raw_bytes: bytes, max_pixels: int, *, mime: str | None = None
) -> Image.Image:
    if raw_bytes.startswith(b"\xff\xd8\xff"):
        expected = "JPEG"
    elif raw_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        expected = "PNG"
    elif raw_bytes.startswith(b"RIFF") and raw_bytes[8:12] == b"WEBP":
        expected = "WEBP"
    else:
        raise EkycError(ErrorCode.INVALID_IMAGE)
    if mime is not None and mime != Image.MIME.get(expected, f"image/{expected.lower()}"):
        raise EkycError(ErrorCode.INVALID_IMAGE)

    image = None
    checked = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(raw_bytes), formats=[expected])
        width, height = image.size
        if min(width, height) < 2:
            raise EkycError(ErrorCode.INVALID_IMAGE)
        if max(width, height) > 16384 or width * height > max_pixels:
            raise EkycError(ErrorCode.IMAGE_TOO_LARGE)
        if image.format != expected or getattr(image, "n_frames", 1) != 1:
            raise EkycError(ErrorCode.INVALID_IMAGE)
        checked = True
        return image
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise EkycError(ErrorCode.IMAGE_TOO_LARGE) from None
    except (OSError, ValueError, SyntaxError, OverflowError):
        raise EkycError(ErrorCode.INVALID_IMAGE) from None
    finally:
        if image is not None and not checked:
            image.close()


def decode_and_transpose_image(
    raw_bytes: bytes, max_pixels: int, url: str | None = None
) -> LoadedImage:
    try:
        with open_checked_image(raw_bytes, max_pixels) as image:
            image.load()
            with ImageOps.exif_transpose(image) as oriented, oriented.convert("RGB") as rgb_image:
                rgb = np.array(rgb_image, dtype=np.uint8)
    except (OSError, ValueError, SyntaxError, OverflowError):
        raise EkycError(ErrorCode.INVALID_IMAGE) from None
    height, width = rgb.shape[:2]
    return LoadedImage(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), rgb, width, height, raw_bytes, url)


async def load_image_from_url(
    url: str,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
) -> LoadedImage:
    """Download image from whitelisted URL, enforce limits, rotate EXIF and decode (async)."""
    with stage("download_ms"):
        raw_bytes = await fetch_image_bytes(url, settings, client=client)
    with stage("decode_ms"):
        return decode_and_transpose_image(raw_bytes, settings.max_image_pixels, url=url)


def load_image_from_url_sync(
    url: str,
    settings: Settings,
    client: httpx.Client | None = None,
) -> LoadedImage:
    """Download image from whitelisted URL, enforce limits, rotate EXIF and decode (sync)."""
    with stage("download_ms"):
        raw_bytes = fetch_image_bytes_sync(url, settings, client=client)
    with stage("decode_ms"):
        return decode_and_transpose_image(raw_bytes, settings.max_image_pixels, url=url)


def load_image_from_bytes(
    raw_bytes: bytes,
    settings: Settings,
) -> LoadedImage:
    """Decode, enforce byte and pixel limits, and auto-rotate EXIF from raw bytes."""
    if len(raw_bytes) > settings.max_image_bytes:
        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)
    return decode_and_transpose_image(raw_bytes, settings.max_image_pixels)
