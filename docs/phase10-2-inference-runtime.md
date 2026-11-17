# Phase 10.2 — Persistent inference runtime

## Audit and design

Phase 10.1 already kept one isolated process for the API. The driver demo
still launched one executable per QR pass and one per OCR pass. Each OCR
invocation recreated the reader and card preparer. Phase 10.2 uses the same
process supervisor in both applications. Each API or demo process owns a
separate child. A child handles one inference job at a time and retains its
detector and OCR adapter. Request payload and result objects are released
when dispatch returns. Existing application review records still follow the
demo's storage policy.

The demo preserves separate QR and OCR calls and their existing budgets of
8 and 35 seconds. Those calls normally run on the same child, so a QR
result received before a later OCR timeout remains available.
The API uses its existing CCCD deadline for each job and keeps the existing
response contracts.

## Runtime controls

- `MAX_CONCURRENT_JOBS` caps active plus waiting jobs per supervisor. With
  the default of 2, one executes and one may wait. A full queue returns
  `ENGINE_BUSY` immediately. The admitted waiter has the smaller of
  `JOB_SLOT_TIMEOUT_S` and its remaining job budget. Demo keeps its existing
  HTTP extraction gate and rate limiter.
- Model construction defaults to lazy. Set `INFERENCE_WARM_UP=true` to
  initialize the detector and OCR adapter during application lifespan
  startup. Startup fails if that explicit warm-up fails. The budget remains
  `CCCD_TIMEOUT_S`. Following a crash, the next admitted request starts a
  replacement and initializes the models it uses.
- `INFERENCE_OCR_FACTORY=package.module:build_engine` is an optional trusted
  deployment setting. The factory accepts `Settings` and returns an
  `OcrEngine` adapter. It can supply a configured VietOCR adapter and retain
  its predictor in the child. The default is EasyOCR. The optional adapter
  test uses a synthetic engine, and the locked experimental VietOCR/Hybrid
  routing was not executed or changed.
- Timeout terminates and reaps the child. The watchdog covers pipe sends as
  well as inference. Crash cleans up the process and pipe. Closing the
  supervisor rejects admission and interrupts an active response wait.
- `GET /health` remains liveness. `GET /health/ready` reports availability
  and state without request or document content. Lazy/recoverable means the
  instance accepts work and will initialize on demand. Warm means startup
  warm-up completed for the current child. An instance with inference
  disabled returns 503 for readiness.

## Measurements

INFO logs include the operation and outcome, total time, queue wait, worker
startup, detector/OCR initialization, decode, QR, card preparation
(detection/warp/quality), OCR and parser time. Each stage is summed across
attempts. Worker restart count is logged on replacement spawn. Peak RAM is
the child process lifetime high-water mark from Windows or Unix process
counters. A killed child may not send its final stage or memory counters.
Logs omit images, raw OCR output, QR payloads and document fields. The
existing API ingress overhead is outside the worker timer.

## Validation

The synthetic tests exercise explicit warm-up and engine reuse, liveness
and readiness, queue overflow, timeout during a blocked engine call, an
actual child exit, restart, shutdown and process reaping. The timeout
scenario uses an 8-second budget and an engine that blocks for 60 seconds.
Shutdown runs while a job is active and another awaits the worker. The
tests assert both callers leave and the child PID is absent afterward.

Ruff, Python compile checks, 413 Pytest cases and 17 frontend Node cases
passed locally. No real documents were submitted. Next environment check
is a synthetic request on the intended deployed runtime to verify worker
spawn behavior and size `CCCD_TIMEOUT_S` and memory for that environment.

## Windows CPU benchmark

Reproduce with `uv run python scripts/benchmark_inference_runtime.py`.
This run used one generated PNG, the existing YOLO checkpoint, and locally
cached EasyOCR weights. Cold means first image with OS caches as they were.
Sequential medians use three images after the first. Each image makes one
QR call and one OCR call. Timings exclude HTTP and database work.

| Scenario | Phase 10.1 one-shot demo dispatch | Phase 10.2 persistent dispatch |
| --- | ---: | ---: |
| First image | 15.64 s | 13.72 s |
| Next three images | 14.86 / 15.36 / 17.78 s | 6.61 / 6.61 / 6.67 s |
| Median of the next three | 15.36 s | 6.61 s |
| Three concurrent callers | 3 finished in 22.86–22.88 s each | 1 finished in 6.81 s, 2 returned ENGINE_BUSY at 0 and 0.61 s |

The concurrent comparison invokes the dispatchers directly. The earlier
demo HTTP route already serialized extraction and would not admit all
three simultaneous requests. The direct test shows the new supervisor's
independent backpressure rather than comparing HTTP throughput.

The cold persistent run measured approximately 1.97 s detector init,
2.00 s OCR init and 6.73 s OCR. Warm OCR took approximately 6.36–6.42 s.
The worker reached 1,154.84 MiB peak RAM by the sequential runs. The
one-shot comparator did not capture peak RAM. It releases its child
after each call, whereas the new worker continues holding its models.
These measurements are local and describe runtime only.

| Recovery case | Phase 10.1 behavior | Phase 10.2 observed test |
| --- | --- | --- |
| Timeout | API reaped its child. Demo subprocess timeout preserved the other source. | Blocked OCR killed, process and pipe cleared, next request successful. |
| Crash | Demo used a fresh process next call. API could restart after EOF. | Abrupt child exit returned INTERNAL_ERROR and next request started a new child. |
| Queue full | Demo HTTP gate already refused concurrent extraction. | Third supervisor caller got ENGINE_BUSY while one executed and one waited. |
| Shutdown | API closed its worker after acquiring the request lock. Demo had no resident child. | Active and queued callers exited, child PID reaped. |

The lifecycle tests use a fake engine so the fault and timing are
deterministic. They do not estimate fault frequency on the deployed runtime.
