# Phase 10.8 — Assisted face comparison and selfie quality

**Status: EXPERIMENTAL / INSUFFICIENT_EVIDENCE.** The feature is off by
default and is blocked in production. The staged API stores no biometric
features or scores. A review decision remains a separate admin action.

## Stack and license audit

The existing service had a face-engine placeholder, DeepFace and
TensorFlow dependencies, and cached ArcFace and RetinaFace weights. There
was no face adapter, license provenance for those cached artifacts, or
validation dataset for CCCD/GPLX portraits versus local selfies.
[DeepFace](https://github.com/serengil/deepface) licenses the wrapper under
MIT. Its license does not establish rights for arbitrary model weights.
[InsightFace](https://github.com/deepinsight/insightface#license) describes
non-commercial restrictions for its published pretrained artifacts. The
cached weights were not loaded or treated as approved assets.

The new local adapter reuses OpenCV and supports operator-supplied ONNX
files for [YuNet](https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_detection_yunet/README.md)
(MIT for the OpenCV Zoo files) and
[SFace](https://raw.githubusercontent.com/opencv/opencv_zoo/main/models/face_recognition_sface/README.md)
(Apache 2.0 for the OpenCV Zoo files). Review the applicable notices and
provenance for the actual files provisioned. The adapter requires local
paths within the manifest directory, SHA-256 pins and a provenance
reference. No model or image was downloaded or sent to a third party.

## Experimental workflow

In a non-production environment, opt in with
`FACE_COMPARISON_ENABLED=true` and `FACE_COMPARISON_MANIFEST` pointing to a
local JSON manifest. The manifest records:

- `detector` and `recognizer` ONNX filenames and their SHA-256 digests.
- `detector_max_side` and `detector_min_score` for the chosen detection
  setup. These are detector parameters.
- `provenance_reference` and a versioned `quality` profile containing
  `min_face_pixels`, `min_blur_score`, `max_glare_fraction`,
  `min_brightness` and optionally `analysis_max_side`.

The adapter runs locally with the existing isolated worker. Face jobs have
a separate admission gate before opening up to three stored images. The
worker lifecycle and hard timeout already apply. A missing manifest keeps
the feature off. Incompatible or unverifiable model files return
`NOT_EVALUATED` without loading substitute weights.

The adapter finds a unique face in the native rectified card and in the
EXIF-normalized selfie. Screening checks insufficient face size, clipped
and unreliable landmarks, exposure, glare and blur. Multiple detections
cause abstention for that source. The landmarks can indicate that areas
are unclear, so reviewers are prompted to check for obstruction. This
build lacks a dedicated or validated occlusion detector.

The API returns these pairs:

- CCCD portrait and selfie.
- GPLX portrait and selfie.
- CCCD portrait and GPLX portrait.

Each pair has a status and, when technically comparable, an uncalibrated
cosine score. A missing source produces `NOT_EVALUATED`. A failed quality
check produces `INSUFFICIENT_QUALITY`. Comparable sources produce
`INCONCLUSIVE`, including at the maximum cosine score. No documented
OpenCV demonstration threshold is adopted for this domain.
`thresholdVersion` stays null. `POSSIBLE_MISMATCH` and
`SIMILARITY_SUPPORTED` are reserved for a future validated policy.
Static-image liveness always returns `NOT_EVALUATED`.

## Consent and access

Expo exposes a separate optional consent toggle only when the configured
API advertises this capability. The consent version and timestamp are
recorded on the submitted snapshot. Declining leaves the regular manual
review and submission flow available. Submit retries reuse the same
consent version. A new submission requires its own consent.

The admin endpoint accepts only references to the current submitted
revision. It checks the Firebase admin claim, application state, receipt,
and revisions before reading captures and rechecks revision after
inference. The result is no-store and visible in goAdmin only for the
current review. Audit keeps actor, timestamp, access action and revision
metadata. Images stay under the existing encrypted-store and retention
rules. Embeddings, crops, scores and landmarks are not persisted or
written to logs. The browser holds the result only in the active review
panel.

## Verification and limits

Backend Ruff, Python compile and all 446 Pytest tests passed. Expo
architecture, typecheck, lint and all 172 Node tests passed. goAdmin
typecheck, lint and four focused review tests passed.

Tests use mock feature vectors and generated non-face fixtures to cover
the three pairs, consent and role gates, current revision, no face,
multiple faces, clipping, poor landmarks, size, glare, blur, model hash
rejection and score abstention. No accuracy, production readiness or
liveness performance was inferred.

Before approving real-data evaluation, verify artifact provenance, the
notice and data handling, and define a separately authorized evaluation
covering the three capture pairs, including obstruction and quality
failures. A future calibrated policy needs independent evidence about
false matches, false nonmatches and abstention. This phase ends with
production disabled.
