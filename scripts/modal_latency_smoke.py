"""Measure a private Modal eKYC endpoint with generated images and aggregate output."""

import argparse
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter
from urllib.parse import urlsplit

import cv2
import httpx
import numpy as np


def synthetic_image(width: int, height: int) -> bytes:
    image = np.full((height, width, 3), 220, dtype=np.uint8)
    cv2.rectangle(
        image, (width // 12, height // 8), (width * 9 // 10, height * 9 // 10), (50, 80, 125), 4
    )
    cv2.putText(
        image,
        "SYNTHETIC TEST",
        (width // 4, height // 2),
        cv2.FONT_HERSHEY_SIMPLEX,
        width / 800,
        (32, 32, 32),
        3,
    )
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("Could not encode the synthetic image")
    return encoded.tobytes()


def timed(client: httpx.Client, path: str, **kwargs) -> tuple[httpx.Response, float]:
    started = perf_counter()
    response = client.request("POST" if "content" in kwargs else "GET", path, **kwargs)
    return response, round((perf_counter() - started) * 1000, 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("origin", help="HTTPS origin of the private Modal endpoint")
    parser.add_argument(
        "--cold-candidate",
        action="store_true",
        help="Mark first probe as a cold candidate after an idle interval",
    )
    args = parser.parse_args()
    origin = urlsplit(args.origin)
    if (
        origin.scheme != "https"
        or not origin.hostname
        or not origin.hostname.endswith(".modal.run")
        or origin.netloc != origin.hostname
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
    ):
        parser.error("Supply an HTTPS .modal.run origin without a path, port or credentials")

    state = Path(os.environ["LOCALAPPDATA"]) / "EkycModalStaging"
    proxy = json.loads((state / "proxy-token.json").read_text(encoding="utf-8-sig"))
    scheme, api_key = (state / "ekyc-key.env").read_text(encoding="utf-8-sig").strip().split("=", 1)
    if scheme != "EKYC_API_KEY":
        raise ValueError("Missing local eKYC staging key")
    with httpx.Client(base_url=args.origin, timeout=90, trust_env=False) as client:
        unauthorized, _ = timed(client, "/health/ready")
        if unauthorized.status_code != 401:
            raise SystemExit(
                f"Expected unauthenticated probe to return 401, got {unauthorized.status_code}"
            )
        client.headers.update(
            {"Modal-Key": proxy["Modal-Key"], "Modal-Secret": proxy["Modal-Secret"]}
        )
        response, first_ms = timed(client, "/health/ready")
        if response.status_code != 200 or response.json().get("worker") != "warm":
            print(json.dumps({"readiness_http": response.status_code, "first_ms": first_ms}))
            raise SystemExit("Warm worker readiness check failed")
        health = [timed(client, "/health")[1] for _ in range(3)]
        samples = []
        for width, height in [(560, 350)] * 3 + [(1600, 1200)]:
            response, elapsed = timed(
                client,
                "/ekyc/documents/cccd_front/extract",
                content=synthetic_image(width, height),
                headers={"X-API-Key": api_key, "Content-Type": "image/png"},
            )
            body = (
                response.json()
                if response.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            samples.append(
                {
                    "size": f"{width}x{height}",
                    "ms": elapsed,
                    "http": response.status_code,
                    "ocr_status": (body.get("data") or {}).get("ocrStatus"),
                    "error_code": body.get("errorCode"),
                }
            )

    print(
        json.dumps(
            {
                "first_readiness_ms": first_ms,
                "cold_candidate": args.cold_candidate,
                "warm_health_median_ms": median(health),
                "warm_health_samples_ms": health,
                "extraction_samples": samples,
            }
        )
    )


if __name__ == "__main__":
    main()
