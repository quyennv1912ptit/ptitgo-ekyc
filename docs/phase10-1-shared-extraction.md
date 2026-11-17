# Phase 10.1 — Shared document extraction

## Audit and design

The CCCD API applied the URL loader, the card preparer with the configured
OpenCV or YOLO backend, geometry and quality gates, EasyOCR, document
classification and the CCCD parser. The driver demo decoded bytes and tried
rotations on the raw frame independently. It stopped trying orientations as
soon as one field parsed. Its QR decoder understood the existing CCCD payload
shape. The classifier and GPLX parser supply signals for assisted extraction,
and the GPLX parser currently returns a single class.

The shared extractor uses the same input loader, preparer, OCR protocol, line
grouping, classifier and parsers. For assisted extraction, QR runs on the
validated original image before card preparation. Geometry or quality rejection
leaves a diagnostic and permits OCR on the original image. Document/side
mismatches from the classifier exclude that OCR view. The strict adapter still
rejects the existing geometry/quality and document validation failures.

## Policies and orientation

- `POST /ekyc/cccd` keeps its two-URL contract, front/back swap, error codes,
  confidence fields and manual review flags. Its existing OCR orientation
  policy retries at 180 degrees on unknown document type when enabled.
  QR is skipped by this policy.
- Assisted extraction evaluates up to two views for a landscape frame and
  three for a portrait frame. It ranks by document and side agreement,
  number of structurally parsed fields and classifier score, with text presence
  as a final signal. Ties and lack of a confirmed type/side produce an
  orientation warning. All values remain suggestions.
- QR and OCR can be invoked independently. The demo retains its separate
  bounded subprocess calls so a late or failed OCR call can preserve QR data.
  Its legacy response keys and field review logic remain available, with
  structured candidates and conflict metadata added to the stored observation.
- The worker holds a reusable EasyOCR adapter. The demo adapter keeps
  downloading disabled. Another adapter can be injected explicitly through
  the OCR protocol or factory. Benchmark routing, model weights and settings
  used to select Hybrid C are outside this change.

## API contract

Send one raw JPEG, PNG or WebP in the authenticated request body to
`POST /ekyc/documents/{slot}/extract` using the existing `X-API-Key` header.
The Phase 10.0 byte and pixel checks apply. The endpoint uses the supervised
worker and returns `Cache-Control: no-store`.

Slots are `cccd_front`, `cccd_back`, `gplx_front` and `gplx_back`.
CCCD back uses the existing issue-date OCR parser. Compatible CCCD QR data
can produce suggestions regardless of card detection. GPLX front supports
OCR and GPLX back reports unsupported for OCR. QR data outside the
supported CCCD format reports unsupported when decoded.

The response includes source statuses, warnings, conflict field keys and
candidates with field key, value, source, document and side, engine confidence
where available, parse status and validation status. Document and side
identify the extraction slot. Structural validation describes parser format
checks, and confidence is an uncalibrated engine signal. In a conflict,
both alternatives are returned and the shared API selects neither.

## Verification and next gate

Tests cover ordering and raw fallback, a QR result surviving OCR failure,
orientation across multiple views, conflict, missing fields, unsupported
GPLX back, API authentication and byte limits, worker dispatch after
timeout recovery, and the existing CCCD and demo submission contracts.
The tests use synthetic data and fake engines.

For a manual check, start the existing local demo, upload a synthetic
sideways card, run extraction, then inspect suggestions and the associated
admin review record. Verify a synthetic conflict remains available for
manual selection. In the deployment runtime, verify the new endpoint with
the same synthetic image and record latency before integrating a new client.
This phase measures behavior with mocks and fixtures. Live device behavior,
full inference latency and accuracy still require separate measurements.
