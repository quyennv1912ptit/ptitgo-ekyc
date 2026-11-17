# Phase 10.3 — Driver verification backend

## Audit and design

The Expo driver's native Firebase Auth configuration uses project
`<firebase-project-id>`. Phase 9's verification screen still calls the separate
demo backend. The main API now has a dedicated versioned Firebase-authenticated
router. The existing API-key CCCD and document extraction routes and the demo
entry point keep their existing auth and request contracts. This phase reuses
the demo's domain fields, required capture slots and name/date comparison.
The new router uses the Phase 10.2 worker for extraction.

## Configuration

Set `FIREBASE_PROJECT_ID=<firebase-project-id>` and
`VERIFICATION_DB_PATH` to a SQLite path on a private, durable volume. Both are
required together. Without either setting the verification feature is disabled.
Firebase Admin uses Application Default Credentials provided by the runtime.
The main API refuses Firebase Auth emulator configuration. Local integration
tests substitute the SDK verification call and do not contact Firebase.

The SDK validates Firebase ID tokens and checks revocation and disabled users on
each authenticated request. The adapter additionally checks issuer, audience,
and subject against the configured project. The token subject is the only
owner UID source. Admin access requires boolean custom claim
`driver_verification_admin` assigned through an authorized server process.
For immediate role removal, revoke the user's sessions after changing the
claim. Admins cannot decide their own application. Users without access to an
owner resource receive 404.

## State and API

Driver: `DRAFT -> SUBMITTED`, or `NEEDS_MORE_INFO -> SUBMITTED`.
Admin: `SUBMITTED -> UNDER_REVIEW`, then
`UNDER_REVIEW -> NEEDS_MORE_INFO | APPROVED | REJECTED`.
Approval and rejection are terminal within this workflow. Rejection and
requests for more information require a reason. Drivers may change captures
and confirmations only in the editable states.

All paths below use prefix `/ekyc/driver-verification/v1` and an
`Authorization: Bearer <Firebase ID token>` header.

| Method and path | Behavior |
| --- | --- |
| GET /me | Return own UID and admin permission |
| POST /applications | Create or return the driver's one application |
| GET /applications | List own application metadata |
| GET /applications/{id} | Read current document, status, revision and audit |
| PUT /applications/{id}/captures/{slot}?revision=N | Stream one JPEG, PNG or WebP |
| GET /applications/{id}/captures/{slot} | Read authorized evidence |
| POST /applications/{id}/extract/{slot} | Run inference on the stored capture with body containing revision |
| PUT /applications/{id}/confirmation | Save body containing revision and details |
| POST /applications/{id}/submit | Submit or resubmit using revision and Idempotency-Key |
| GET /admin/applications | Paginated list of submitted applications |
| GET /admin/applications/{id} | Review the latest submitted snapshot |
| GET /admin/applications/{id}/captures/{slot} | Read submitted evidence |
| POST /admin/applications/{id}/decision | Send revision, target status and reason when required |

Detail and evidence GET routes support query `snapshot=N` for a prior
submission. Application metadata includes the current revision and status.
The response uses the main API envelope, except for binary capture responses.
Check `success` as well as HTTP status because existing field and image error
codes use HTTP 200. The new routes set `Cache-Control: no-store`.

## Integrity and storage

Every mutation compares the revision inside a SQLite write transaction. The
version is checked again when committing extraction after inference, so a
proposal from a replaced capture cannot attach to the new capture. Replacing
a capture invalidates its proposal and requires fresh driver confirmation.
The driver can inspect the prior confirmed values before confirming again.
Capture replacement, field changes, submission and review decisions are
recorded in audit history. The confirmation payload and extraction proposal
are stored separately. Inference cannot change review state.

Submit is idempotent per application, key and expected revision. Same-key
replays return the original response, including after a later decision.
A reused key with a different revision returns a conflict. The submit
transaction writes an immutable snapshot with both source observations and
confirmed data, freezes its capture references, and increments the revision.
The admin reads submitted snapshots instead of unpublished edits.

SQLite stores evidence and confirmation data on disk. Use single-host durable
storage with restricted filesystem access, encryption at rest and a retention
and backup policy before enabling real data. The repository does not provide
a managed database, retention scheduler, or encrypted storage provisioning.
The new store begins empty. Demo records are separate.

## Verification and next gate

Integration tests use synthetic images and a local Firebase SDK test adapter.
They cover missing, expired, revoked and invalid tokens, wrong project,
role and owner isolation, concurrent revision updates, idempotency, state
transitions, stale admin decisions and snapshot preservation on resubmit.
The existing demo and OCR API regression suites also run.

Before switching the app to this router, verify in a staging Firebase project
with real staging-issued tokens, admin claims and revocation. Confirm
durability and permissions of the storage volume and test the capture,
edit, submit and review flow against that staging API. This phase is backend
only. The Expo UI and admin UI still need to be connected to the new routes.
