# Phase 10.5 — Expo native driver verification

## Audit and design

Phase 9.0 supplied a native Expo Router screen, system camera and gallery,
manual fields and a multipart demo client. Phase 10.5 continues the existing
`profile/verification` route and shared UI controls. The Driver app uses
Expo SDK 58 and includes ImagePicker, FileSystem and Firebase Auth.
The current app has no native live camera preview module. ImagePicker
opens the system camera. The app shows the framing guide before capture
and lets the driver inspect the result afterward. No live overlay or
automatic corner, face-count or sharpness check is presented.

Set `EXPO_PUBLIC_DRIVER_VERIFICATION_URL` to the origin of the main eKYC
backend without a path. This enables the link in the profile. Release
builds require HTTPS. Dev builds also allow localhost, Android emulator
loopback, and ADB reverse on localhost. Enable verification storage and
Firebase for the same environment on the backend before testing.

## Capture, extraction and submission

- Five guided capture slots cover both CCCD sides, GPLX front and optional
  back, and selfie. Camera permission failures offer the gallery and a
  settings action when permission cannot be asked for again. An invalid
  file, unsupported MIME, or technical limit blocks the capture. A valid
  small image requires a separate acknowledgment. Lighting, glare, angle
  and framing remain manual checks.
- The existing Firebase UID gates the feature and the client fetches the
  corresponding Firebase ID token for requests. On a 401 it attempts one
  forced token refresh. The transport checks HTTP status and the API
  success envelope, rejects redirects and uses bounded requests.
- Each capture uploads as a raw File through Expo fetch to the Phase 10.4
  PUT endpoint. Submit sends JSON with revision and Idempotency-Key.
  Multipart is a negative compatibility test because the backend rejects
  it. On a mobile retry the same submit key and revision are preserved
  for the current session. Creating a draft uses the backend's one-per-UID
  operation. Busy state prevents repeated in-screen writes.
- QR/OCR runs on uploaded captures. The client combines valid candidates
  by field and source. It fills empty untouched fields when there is one
  eligible candidate and no conflict. The driver can choose a value after
  reviewing conflicting sources, or type manually. Changing a capture or
  the application revision invalidates the previous result. A generation
  check discards a late response. Firebase/OCR failures keep manual editing
  available. Slot-specific warnings identify unsupported sides and uncertain
  document orientation.
- Input preserves numbering strings and Vietnamese diacritics. Name
  comparison distinguishes a diacritic difference from different text.
  DOB is compared separately. GPLX can have several classes and an explicit
  indefinite expiry. An absent expiry remains blank for manual review.
- The driver can preview a local or protected server capture, review
  warnings and fields, correct them, confirm, and submit. The status
  screen reflects backend state and admin reason and permits edits when
  the backend asks for more information. A submission success message is
  shown after the backend confirms the request or its idempotent replay.

## Interruptions and data handling

An interrupted upload or OCR call leaves the in-session image available
for retry. App backgrounding cancels in-flight client operations, and
foregrounding refreshes the application when the client is idle.
A timeout may occur after the server commits a mutation. Reloading the
application and applying the backend revision or retrying the same submit
key reconciles the outcome. Client cancellation does not assert that
server inference stopped.

Pending unuploaded images and edits are session-only. On Android process
destruction, reopen the route, recover saved server state, and recapture
unsaved slots rather than attaching an ambiguous picker result. Temporary
cache images are deleted on replace, submission or screen exit where the
OS has preserved the process. Picker-owned files may still be reclaimed by
the OS after an abrupt process exit. No tokens, applicant details, or images
are written to AsyncStorage by this flow. Protected previews are fetched
with auth and kept only in application cache during review.

The mobile screen gates transfer on session consent. Phase 10.4 still
requires a versioned consent record and approved notice before collecting
real documents.

## Verification and Android device checklist

Automated checks cover logic and transport with synthetic data and the
main API's auth, ownership, raw upload, expiry, revision and resubmit
contracts. ADB detected a physical Android device with the Driver package
installed. **DEVICE_VALIDATION_PENDING** remains because no authenticated
staging session with this updated bundle was exercised.

- Open the current native route with staging Firebase and a synthetic driver.
- Capture each slot from the system camera and gallery. Test refusal and
  permanent denial of camera permission. Confirm optional GPLX back can
  be skipped.
- Upload each raw JPEG/PNG/WebP. Check that multipart and unsupported
  types are rejected and a small valid image asks for acknowledgment.
- Background during upload, OCR and submit. Resume and refresh. Put the
  device offline mid-upload, restore networking and retry.
- Force an OCR timeout. Retake an image while inference is running and
  check that stale suggestions do not reappear. Confirm that a conflict
  retains both choices and a manual field stays intact.
- Submit, retry after a lost response, and use a staging admin session
  to request changes. Replace one image, confirm, and resubmit.
- Test an expired Firebase token, then a revoked session. Confirm one
  refresh attempt and the sign-in message if the session has ended.

Monorepo architecture checks, workspace TypeScript and Expo lint passed.
The monorepo Node suite passed 172 tests. The focused backend API and
storage integration suite passed 10 tests.
