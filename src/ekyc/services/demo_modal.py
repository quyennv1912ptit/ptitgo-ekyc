"""Server-side Modal extraction for the explicitly opted-in synthetic demo."""

import os
from urllib.parse import urlsplit

import httpx

from ekyc.schemas.document import ExtractionResult


def extract_modal(raw: bytes, slot: str) -> ExtractionResult:
    origin = os.environ["DRIVER_DEMO_MODAL_ORIGIN"].rstrip("/")
    url = urlsplit(origin)
    if (
        url.scheme != "https"
        or not url.hostname
        or not url.hostname.endswith(".modal.run")
        or url.netloc != url.hostname
        or url.path
        or url.query
        or url.fragment
    ):
        raise ValueError("Invalid Modal origin")
    with httpx.Client(timeout=90, follow_redirects=False, trust_env=False) as client:
        response = client.post(
            origin + f"/ekyc/documents/{slot}/extract",
            content=raw,
            headers={
                "Content-Type": "application/octet-stream",
                "X-API-Key": os.environ["DRIVER_DEMO_MODAL_API_KEY"],
                "Modal-Key": os.environ["DRIVER_DEMO_MODAL_KEY"],
                "Modal-Secret": os.environ["DRIVER_DEMO_MODAL_SECRET"],
            },
        )
        response.raise_for_status()
        result = response.json()
        if result.get("success") is not True:
            raise ValueError("Modal extraction failed")
        return ExtractionResult.model_validate(result["data"])
