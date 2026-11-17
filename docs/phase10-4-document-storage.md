# Phase 10.4 — Private document storage and data protection

## Inventory and selection

Phase 10.3 kept images as BLOBs in the verification SQLite database. The
loopback demo uses its own BLOB database. The mobile application's existing
upload helper asks for a Cloudinary upload signature and returns a
`secureUrl`. No private identity-evidence configuration was identified for
that integration. This phase uses a private store interface and a local
adapter paired with the existing verification database.

## Upload, ownership and preview

- The existing raw image PUT endpoint accepts one JPEG, PNG or WebP at a
  time. Multipart is rejected before parsing. Bounding bytes, read timeout,
  format checks and pre-decode pixel checks protect ingestion. There are
  at most five active slots per application and one current image per slot.
  The per-owner upload attempt rate is enforced in SQLite. Concurrent
  uploads are bounded per API process.
- The server generates both the capture ID and the object key. SQLite
  records application, owner, slot, capture revision, byte count, MIME and
  SHA-256. Object keys stay internal. Uploads with identical bytes and MIME
  in the current slot are no-ops at the current revision. Stale revision
  requests still return conflict.
- Objects publish through a staging file and an atomic rename. Metadata
  commits inside the optimistic-revision transaction. Rollback attempts
  to remove an unreferenced object. The explicit sweep handles crash orphans
  and abandoned staging files after a grace period.
- Existing authenticated capture endpoints remain the preview mechanism.
  Each read checks the Firebase identity or admin permission, application,
  slot, capture ID and revision view. Admin reads submitted snapshots. The
  server checks the byte count and SHA-256 on object reads. There are no
  public URLs or signed preview URLs. Responses use no-store.
- Legacy Phase 10.3 BLOBs remain readable and untouched. New uploads store
  only references in SQLite. Older submitted revisions keep their original
  capture references when the driver replaces a capture.

## Security configuration and consent

`VERIFICATION_STORAGE_DIR` sets the local object directory. It defaults to
the database name with an `.objects` suffix. Use an empty dedicated directory
on first setup. A binding marker ties it to its database and encryption key.
Production requires HTTPS on verification routes and a
`VERIFICATION_STORAGE_KEY` managed outside the repository. The local adapter
uses authenticated Fernet encryption for objects. Local development can
use plaintext with synthetic data. Configure trusted proxy forwarding and
test HTTPS through the target ingress before enabling traffic.

Before collecting real documents, record the data notice and consent or
other authorized basis established for this workflow, the permitted use,
required capture slots, intended reviewer access, processing location,
retention schedule and deletion/contact process. The app must capture the
notice version and confirmation before upload. This phase supplies the
backend storage controls. The consent UI and consent records are pending.

File-level encryption covers new objects. Use encrypted disks and backups
for the SQLite database, which holds confirmed fields, audit data and any
legacy BLOBs. Keep the database and object directory out of static serving,
restrict filesystem access, configure a key recovery process and test paired
backup/restore. Changing the key on an existing directory needs a supervised
migration. Restrict admin roles and disable request/response body collection
in proxies, error trackers and analytics. No external data transfer occurred
during verification.

## Retention and deletion

`VERIFICATION_RETIRED_DAYS` defaults to 7 for replaced captures that never
appeared in a submission. `VERIFICATION_ORPHAN_HOURS` defaults to 24 for
unreferenced objects and staging files. `VERIFICATION_TERMINAL_DAYS` is opt-in
and applies only to objects from approved or rejected applications, measured
from the terminal decision timestamp. An active or reviewable submission
keeps its evidence. Legacy BLOBs, snapshots, driver details and audit events
are outside this image sweep and need a separate deletion process.

Run `uv run python scripts/verification_storage_retention.py` against the
configured database to preview aggregate counts. The app never schedules
deletion automatically. An authorized operator can pass `--apply` after
reviewing eligibility and any applicable hold. Scheduling creates a
durable audit record before deletion. Failed deletions remain pending for
retry. A successful deletion leaves a tombstone so an old snapshot still
exists and its preview reports 410. Backup expiry must follow the separately
configured backup policy. No sweep was applied to existing project data.

## Validation and next gate

Synthetic tests cover unauthenticated and cross-owner preview, MIME/bytes
mismatch, multipart rejection, rate limits, duplicate upload, stale revision,
encrypted storage, publish failure cleanup, orphan discovery, retention and
tombstones, storage binding, legacy BLOB preservation, and sensitive data
absent from upload logs. Existing resubmission tests exercise historic
evidence reads. Production HTTPS enforcement is tested via TestClient.

Before using real images, test staging ingress, encryption and paired restore,
review consent and retention with the data owner, and verify the controlled
deletion procedure using authorized synthetic fixtures on the intended volume.
