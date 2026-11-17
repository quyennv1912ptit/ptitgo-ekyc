"""Bounded process-local metrics. Labels contain only application constants."""

from collections import Counter, deque
from threading import Lock
from time import monotonic

from ekyc.core.errors import ErrorCode


class RuntimeMetrics:
    def __init__(self):
        self.lock = Lock()
        self.latency = {"request": deque(maxlen=2048), "worker": deque(maxlen=2048)}
        self.requests: Counter[str] = Counter()
        self.errors: Counter[tuple[str, str]] = Counter()
        self.worker: Counter[str] = Counter()
        self.waiting = 0
        self.active = 0
        self.peak_ram = 0.0

    def request(self, duration: float, status: int, code: ErrorCode | None):
        with self.lock:
            self.latency["request"].append(duration)
            self.requests[f"{status // 100}xx"] += 1
            if code is not None:
                self.errors[(error_stage(code), code.value)] += 1
                if status == 200:
                    self.requests["business_error"] += 1

    def queue(self, *, waiting=0, active=0):
        with self.lock:
            self.waiting += waiting
            self.active += active

    def outcome(self, code: str, duration: float, peak: float | None):
        with self.lock:
            self.worker[code] += 1
            self.latency["worker"].append(duration)
            if peak is not None:
                self.peak_ram = max(self.peak_ram, peak)

    def restart(self):
        with self.lock:
            self.worker["restart"] += 1

    def render(self) -> str:
        def labeled(metric: str, label: str, counts: Counter) -> list[str]:
            return [f'{metric}{{{label}="{key}"}} {value}' for key, value in sorted(counts.items())]

        with self.lock:
            rows = [
                f"ekyc_inference_queue_depth {self.waiting}",
                f"ekyc_inference_active {self.active}",
                f"ekyc_inference_worker_peak_ram_mib {self.peak_ram}",
                f'ekyc_inference_events_total{{outcome="restart"}} {self.worker["restart"]}',
                *labeled("ekyc_http_responses_total", "status", self.requests),
                *labeled(
                    "ekyc_inference_events_total",
                    "outcome",
                    Counter({key: value for key, value in self.worker.items() if key != "restart"}),
                ),
            ]
            rows.extend(
                f'ekyc_errors_total{{stage="{stage}",code="{code}"}} {value}'
                for (stage, code), value in sorted(self.errors.items())
            )
            for kind, values in self.latency.items():
                ordered = sorted(values)
                if not ordered:
                    continue
                for percentile in (50, 95, 99):
                    index = (percentile * len(ordered) + 99) // 100 - 1
                    rows.append(
                        f'ekyc_latency_seconds{{kind="{kind}",quantile="0.{percentile}"}} '
                        f"{ordered[index]:.6f}"
                    )
        return "\n".join(rows) + "\n"


def error_stage(code: ErrorCode) -> str:
    value = code.value
    if value in {"UNAUTHORIZED", "FORBIDDEN"}:
        return "authorization"
    if value in {"URL_NOT_ALLOWED", "IMAGE_FETCH_FAILED"}:
        return "fetch"
    if value.startswith("ENGINE_") or value == "MODEL_UNAVAILABLE":
        return "inference"
    if value.startswith(("CARD_", "IMAGE_")) or value in {"MULTIPLE_CARDS", "INVALID_IMAGE"}:
        return "preparation"
    if value.startswith(("FIELD_", "INVALID_FIELD", "DOCUMENT_")):
        return "validation"
    return "request"


metrics = RuntimeMetrics()


class MetricsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in {
            "/ekyc/metrics",
            "/health",
            "/health/ready",
        }:
            await self.app(scope, receive, send)
            return
        started = monotonic()
        status = 500

        async def observe(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, observe)
        finally:
            code = scope.get("state", {}).get("ekyc_error_code")
            metrics.request(monotonic() - started, status, code)
