# Phase 10.6 — Driver verification review in goAdmin

## Integration

The admin interface lives in the existing goAdmin Vite/React application.
Its current **Duyệt hồ sơ** page uses the ride-management backend and remains
available. It links to **Xác minh hồ sơ từ ứng dụng tài xế** at
`/driver-documents/verification` when the eKYC origin is configured. The
review page reuses the existing Firebase session, layout and Ant Design
components. It requires no pasted demo token and no separate dashboard.

- Set `VITE_EKYC_BASE_URL` in goAdmin to the eKYC origin with no path.
- Set `VERIFICATION_ADMIN_ORIGINS` in eKYC to the exact goAdmin frontend
  origins as a JSON array, for example `["http://localhost:5173"]` in local
  development. Use the deployed HTTPS origin in production.
- The Firebase project must match across goAdmin and eKYC. The signed boolean
  claim `driver_verification_admin` is required in addition to the existing
  goAdmin login. Set it through the authorized Firebase admin workflow and
  refresh the session before checking permissions. Config examples contain
  names and origins only.

## Review flow and data boundaries

The queue searches by exact application UUID and filters status server-side.
It returns up to 100 items per page and indicates whether there is another
page. Open cases sort first, followed by warning count and time submitted.
The list contains metadata only, including the latest submission time,
number of resubmissions and warning count. It omits driver fields, reasons
and image data.

Details compare QR/OCR candidates with values the driver confirmed. The UI
marks conflicts and confirmed values that differ from all proposals.
When a previous submission exists, it marks fields changed from that
submission. Reviewers can inspect each submitted revision and its evidence.
Image dimensions and small-image warnings apply to newly uploaded images.
The UI translates saved extraction status and warnings into Vietnamese.
Old evidence may lack measurements. If an extraction did not produce a saved
result, the screen reports that no extraction record is available.
Face matching is displayed as unavailable.

Images load on demand from the authorized evidence endpoint, pinned to the
viewed submission. The browser creates a temporary object URL for preview
and zoom and revokes it when the image leaves the view. The API returns
no-store. Neither the queue nor the UI provides public URLs or a bulk export.

## Decisions and resubmission

Reviewers start review on a submitted application, then approve, reject or
request more information. The UI requires confirmation that the current
submission was viewed. Reject and request-more-info require a reason.
A request also identifies at least one capture slot or field. Expiry date
and indefinite expiry are treated as one field pair.

The API checks authorization, current revision and valid transition in the
write transaction and records actor, action, revision, time, reason and
requested targets in audit history. On a conflict, the reviewer must reopen
the current revision. The admin cannot decide their own application.
An OCR warning or score has no approval authority.

The Expo driver displays the requested targets and limits edit controls.
The backend independently enforces the same limits during upload, extraction
and confirmation. Requested captures need a new stored image before
resubmission. Requested fields can be corrected or confirmed after review.
Other fields and evidence remain as submitted. Existing historical
requests that predate structured targets retain their original editing
behavior. Resubmission keeps older evidence and confirmed values available
through immutable snapshots.

## Validation

Synthetic API tests cover authorization, both final decisions, structured
requests received by the driver, forbidden edits, replacement, resubmission,
concurrent review and stale revisions. The goAdmin tests cover source
comparison, access-denied feedback and sending the reviewed revision with
a required reason. Expo architecture, typecheck, lint and logic tests cover
the driver changes.

Before enabling this route in a shared environment, test with a staging
Firebase admin claim and a synthetic Expo submission. Verify private
preview/zoom, CORS, review in two browser sessions, request one capture
and one field, and resubmit using the mobile client. Readiness for real
documents still depends on the consent and storage controls in Phase 10.4.

Local results: 424 backend tests passed with Ruff and Python compile
checks. goAdmin passed build, lint, and all 84 web tests. Under Node 26 the
full web suite used `NODE_OPTIONS=--no-experimental-webstorage` so jsdom
supplied storage to the existing theme tests. Expo monorepo architecture,
typecheck, lint and all 172 Node tests passed. Live staging Firebase and
physical-device review were outside these local checks.
