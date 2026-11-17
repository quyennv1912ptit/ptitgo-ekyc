"""Post-deploy verification for a private Modal eKYC deployment using synthetic data only."""

from __future__ import annotations

import argparse
import json
import os
import sys
from time import monotonic, sleep
from urllib.parse import urlsplit

import cv2
import httpx
import numpy as np

UNREADY_FATAL_CODES = frozenset(
    {
        "MODEL_FILE_MISSING",
        "MODEL_HASH_MISMATCH",
        "MODEL_DEPENDENCY_MISSING",
        "MODEL_DEPENDENCY_MISMATCH",
        "MODEL_CONFIG_UNSUPPORTED",
        "MODEL_WARMUP_FAILED",
    }
)
FORBIDDEN_SMOKE_ERRORS = frozenset(
    {
        "INTERNAL_ERROR",
        "MODEL_UNAVAILABLE",
        "UNAUTHORIZED",
        "FORBIDDEN",
    }
)


def validate_modal_origin(raw_origin: str) -> str:
    cleaned = raw_origin.strip().rstrip("/")
    parsed = urlsplit(cleaned)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".modal.run")
        or parsed.netloc != parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Supply an HTTPS .modal.run origin without a path, port, or credentials")
    return cleaned


def synthetic_image(width: int = 560, height: int = 350) -> bytes:
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
        raise RuntimeError("Could not encode synthetic PNG image")
    return encoded.tobytes()


def verify_deployment(
    client: httpx.Client,
    *,
    proxy_key: str,
    proxy_secret: str,
    api_key: str,
    expected_sha: str | None = None,
    expected_version: str | None = None,
    ready_timeout_s: float = 120.0,
    poll_interval_s: float = 2.0,
) -> dict[str, object]:
    unauth = client.get("/health/ready")
    if unauth.status_code != 401:
        raise RuntimeError(
            f"Expected unauthenticated /health/ready to return 401, got {unauth.status_code}"
        )

    proxy_headers = {"Modal-Key": proxy_key, "Modal-Secret": proxy_secret}

    health = client.get("/health", headers=proxy_headers)
    if health.status_code != 200:
        raise RuntimeError(f"/health returned HTTP {health.status_code}")
    health_body = health.json()
    if health_body.get("status") != "ok":
        raise RuntimeError(f"Unexpected /health status: {health_body.get('status')!r}")
    if expected_sha and health_body.get("commitSha") != expected_sha:
        raise RuntimeError(
            f"Commit SHA mismatch: expected {expected_sha!r}, got {health_body.get('commitSha')!r}"
        )
    if expected_version and health_body.get("version") != expected_version:
        raise RuntimeError(
            f"Version mismatch: expected {expected_version!r}, got {health_body.get('version')!r}"
        )

    deadline = monotonic() + ready_timeout_s
    ready_body: dict[str, object] = {}
    ready_code = 0
    while True:
        ready = client.get("/health/ready", headers=proxy_headers)
        ready_code = ready.status_code
        ready_body = (
            ready.json()
            if ready.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        if ready_code == 200 and ready_body.get("status") == "ready":
            break
        err_code = ready_body.get("code")
        if err_code in UNREADY_FATAL_CODES:
            raise RuntimeError(f"Model readiness check failed with {err_code}")
        if monotonic() >= deadline:
            raise RuntimeError(
                f"Service did not become ready within {ready_timeout_s}s "
                f"(http={ready_code}, worker={ready_body.get('worker')}, code={err_code})"
            )
        sleep(poll_interval_s)

    if ready_body.get("worker") not in {"warm", "started", "busy"}:
        raise RuntimeError(f"Unexpected worker state: {ready_body.get('worker')!r}")

    no_key_metrics = client.get("/ekyc/metrics", headers=proxy_headers)
    if no_key_metrics.status_code != 401:
        raise RuntimeError(
            "Expected /ekyc/metrics without X-API-Key to return 401, "
            f"got {no_key_metrics.status_code}"
        )
    bad_key_metrics = client.get(
        "/ekyc/metrics",
        headers={**proxy_headers, "X-API-Key": "invalid-smoke-probe-key-value"},
    )
    if bad_key_metrics.status_code != 401:
        raise RuntimeError(
            "Expected /ekyc/metrics with bad X-API-Key to return 401, "
            f"got {bad_key_metrics.status_code}"
        )
    no_key_extract = client.post("/ekyc/documents/cccd_front/extract", headers=proxy_headers)
    if no_key_extract.status_code != 401:
        raise RuntimeError(
            f"Expected /ekyc/documents/cccd_front/extract without X-API-Key to return 401, "
            f"got {no_key_extract.status_code}"
        )

    valid_metrics = client.get(
        "/ekyc/metrics",
        headers={**proxy_headers, "X-API-Key": api_key},
    )
    if valid_metrics.status_code != 200 or "ekyc_inference_" not in valid_metrics.text:
        raise RuntimeError(
            f"Authenticated /ekyc/metrics check failed: HTTP {valid_metrics.status_code}"
        )

    smoke = client.post(
        "/ekyc/documents/cccd_front/extract",
        content=synthetic_image(),
        headers={
            **proxy_headers,
            "X-API-Key": api_key,
            "Content-Type": "image/png",
        },
    )
    if smoke.status_code != 200:
        raise RuntimeError(f"Synthetic smoke request failed with HTTP {smoke.status_code}")
    smoke_body = smoke.json()
    if "requestId" not in smoke_body or "success" not in smoke_body:
        raise RuntimeError("Synthetic smoke response missing standard API envelope fields")
    smoke_err = smoke_body.get("errorCode")
    if smoke_err in FORBIDDEN_SMOKE_ERRORS:
        raise RuntimeError(f"Synthetic smoke failed with technical/auth error: {smoke_err}")

    return {
        "health": "ok",
        "ready": ready_body.get("status"),
        "worker": ready_body.get("worker"),
        "version": health_body.get("version"),
        "commitSha": health_body.get("commitSha"),
        "internalAuth": "enforced",
        "smokeHttp": smoke.status_code,
        "smokeSuccess": smoke_body.get("success"),
        "smokeErrorCode": smoke_err,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=os.environ.get("MODAL_ENDPOINT_URL", ""))
    parser.add_argument("--expected-sha", default=os.environ.get("GIT_COMMIT_SHA", ""))
    parser.add_argument("--expected-version", default=os.environ.get("MODAL_DEPLOY_TAG", ""))
    parser.add_argument("--ready-timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    origin = validate_modal_origin(args.origin)
    proxy_key = os.environ.get("MODAL_PROXY_KEY", "").strip()
    proxy_secret = os.environ.get("MODAL_PROXY_SECRET", "").strip()
    api_key = (
        os.environ.get("EKYC_SMOKE_API_KEY", "").strip()
        or os.environ.get("EKYC_API_KEY", "").strip()
    )
    if not proxy_key or not proxy_secret:
        raise RuntimeError("MODAL_PROXY_KEY and MODAL_PROXY_SECRET are required for verification")
    if not api_key:
        raise RuntimeError("EKYC_SMOKE_API_KEY (or EKYC_API_KEY) is required for verification")

    with httpx.Client(base_url=origin, timeout=90.0, trust_env=False) as client:
        report = verify_deployment(
            client,
            proxy_key=proxy_key,
            proxy_secret=proxy_secret,
            api_key=api_key,
            expected_sha=args.expected_sha.strip() or None,
            expected_version=args.expected_version.strip() or None,
            ready_timeout_s=args.ready_timeout,
        )
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
