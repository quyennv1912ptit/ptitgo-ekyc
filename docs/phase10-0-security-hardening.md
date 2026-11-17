# Phase 10.0 — Security and input safety

## Audit and design

The CCCD URL loader followed redirects before checking the destination. Pillow
decoded pixels before checking the configured pixel limit and changed a global
Pillow limit per image. The CCCD request thread checked time only after OCR
returned. The local driver demo already bounded streamed request bodies and
individual parts and ran extraction in subprocesses. Its image validation was
separate. The Docker ignore file existed locally but Git ignored it.

Keep the existing CCCD API and business pipeline. Check the URL and image at
their input boundaries. Run the existing CCCD pipeline in an isolated process
so the API can enforce a deadline through DNS, download, decode, detection,
OCR and parsing.

## Changes

- `image_loader.py` checks the scheme, default port, canonical DNS hostname
  and allowlist before resolution or transport. Credentials, literal IP URLs
  and unusual hostname syntax are rejected. Any forbidden address in the DNS
  response rejects the request, including mixed public/private responses.
  The connection targets the checked numeric address with the original HTTP
  Host and TLS server name. The production client ignores proxy environment
  configuration. The redirect limit is zero. HTTP responses are checked before
  reading the body and non-identity content encoding is refused.
- The same module checks JPEG/PNG/WebP signatures and Pillow format, validates
  dimensions and pixel count before decode, rejects multiple frames and handles
  decompression bomb warnings and errors. The limit is 16,384 pixels per side
  in addition to the configured area limit. CCCD preserves EXIF transpose.
  The local demo reuses the header validation and checks declared MIME while
  retaining its upload limits.
- `cccd_worker.py` owns one reusable spawned process per API process. The
  deadline starts at entry to the CCCD service wrapper, includes its queue and
  cold start, and is checked across the internal stages. If the running job
  misses the deadline, the API kills and joins the worker, closes its pipe and
  releases its slot. The next job can start a new worker. Stage failures keep
  their public error codes. Engine initialization moved to the first request
  and has a cold-start cost. Jobs in this API process execute serially.
- `.dockerignore` allows only Docker/package metadata and Python source under
  `src/ekyc`. The YOLO checkpoint is still included by the explicit Modal
  `add_local_file` call. `.gitignore` now permits tracking `.dockerignore`.

## Verification

- Ruff, Pytest and Python compile checks run locally.
- Regression cases cover redirects to outside and internal destinations,
  forbidden IPv4/IPv6 DNS answers, byte limits, fake extensions, pre-decode
  pixel limits, animated PNG, and killing/reaping and restarting a process
  with deliberately blocked OCR. Existing HTTP tests cover the API contract.
- Docker Desktop BuildKit exported an actual context via a scratch build
  using the root ignore file. The export contained 56 allowed files. The
  checkpoint used by the Modal entrypoint exists locally.

## Limits and next gate

The sync HTTP client has per-operation timeouts and checks the download time
budget between I/O operations. A blocking OS DNS lookup or socket operation
can overrun that smaller budget until it returns or the supervised CCCD
deadline kills the process. Direct callers of the sync loader do not have
that outer process watchdog. The CCCD deadline starts after FastAPI parses
the JSON body. HTTP ingress/body time limits belong at the deployment edge.

Before connecting the next phase to a deployed instance, smoke test a synthetic
request against the actual Linux/Modal runtime to check process spawning,
cold-start timing and TLS against the configured image host. The scratch build
did not build the complete runtime image or execute actual model inference.
