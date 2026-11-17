"""Transactional storage for driver verification and immutable submissions."""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.services.document_storage import DocumentStorage, LocalDocumentStorage
from ekyc.services.driver_auth import Actor
from ekyc.services.driver_domain import (
    REQUIRED,
    SLOTS,
    Details,
    canonical_details,
    comparison_warnings,
)
from ekyc.services.face_comparison import CONSENT_VERSION, enabled
from ekyc.services.face_comparison import SLOTS as FACE_SLOTS
from ekyc.services.verification_warnings import FACE_QUALITY_MESSAGES, verification_warnings

EDITABLE = {"DRAFT", "NEEDS_MORE_INFO"}
TRANSITIONS = {
    "SUBMITTED": {"UNDER_REVIEW"},
    "UNDER_REVIEW": {"NEEDS_MORE_INFO", "APPROVED", "REJECTED"},
    "APPROVED": {"REVOKED", "NEEDS_MORE_INFO"},
    "REVOKED": {"NEEDS_MORE_INFO"},
}


def now() -> str:
    return datetime.now(UTC).isoformat()


class VerificationStore:
    @classmethod
    def from_settings(cls, settings: Settings):
        path = settings.verification_db_path
        if path is None:
            raise ValueError("Configure the verification database")
        key = settings.verification_storage_key
        if settings.deployed and key is None:
            raise ValueError("Production verification storage requires encryption")
        storage = LocalDocumentStorage(
            settings.verification_storage_dir or path.with_suffix(".objects"),
            key.get_secret_value() if key else None,
        )
        store = cls(
            path,
            storage,
            license_classes=settings.license_class_codes,
            face_enabled=enabled(settings),
        )
        with store.connect(write=True) as db:
            db.execute("CREATE TABLE IF NOT EXISTS storage_binding (id TEXT PRIMARY KEY)")
            db.execute(
                "INSERT INTO storage_binding SELECT ? "
                "WHERE NOT EXISTS (SELECT 1 FROM storage_binding)",
                (uuid4().hex,),
            )
            binding = db.execute("SELECT id FROM storage_binding").fetchone()["id"]
        with store.connect(write=True):
            storage.bind(binding)
        return store

    def __init__(
        self, path: Path, storage: DocumentStorage, *, license_classes=None, face_enabled=False
    ):
        self.license_classes = frozenset(
            Settings.model_fields["license_class_codes"].default
            if license_classes is None
            else license_classes
        )
        self.face_enabled = face_enabled
        self.path = path.resolve()
        if self.path.is_relative_to((Path(__file__).resolve().parents[3] / "frontend").resolve()):
            raise ValueError("Verification database must be outside the web root")
        self.storage = storage
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS verification_face_status (
                    application_id TEXT NOT NULL, submission INTEGER NOT NULL,
                    statuses TEXT NOT NULL,
                    PRIMARY KEY (application_id, submission)
                );
                CREATE TABLE IF NOT EXISTS applications (
                    id TEXT PRIMARY KEY, owner TEXT UNIQUE NOT NULL, status TEXT NOT NULL,
                    revision INTEGER NOT NULL, payload TEXT NOT NULL, reason TEXT,
                    submission INTEGER, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id),
                    mime TEXT NOT NULL, data BLOB NOT NULL, submitted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS document_objects (
                    id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(id),
                    owner TEXT NOT NULL, slot TEXT NOT NULL, created_revision INTEGER NOT NULL,
                    mime TEXT NOT NULL, bytes INTEGER NOT NULL, checksum TEXT NOT NULL,
                    object_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
                    retired_at TEXT, first_submitted_revision INTEGER
                );
                CREATE INDEX IF NOT EXISTS document_application ON document_objects(application_id);
                CREATE TABLE IF NOT EXISTS document_deletions (
                    object_key TEXT PRIMARY KEY, reason TEXT NOT NULL, scheduled_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS upload_attempts (
                    owner TEXT NOT NULL, at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS upload_attempt_time ON upload_attempts(at);
                CREATE INDEX IF NOT EXISTS upload_attempt_owner ON upload_attempts(owner, at);
                CREATE TABLE IF NOT EXISTS submissions (
                    application_id TEXT NOT NULL REFERENCES applications(id),
                    revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (application_id, revision)
                );
                CREATE TRIGGER IF NOT EXISTS no_submission_update BEFORE UPDATE ON submissions
                    BEGIN SELECT RAISE(ABORT, 'Immutable submission'); END;
                CREATE TRIGGER IF NOT EXISTS no_submission_delete BEFORE DELETE ON submissions
                    BEGIN SELECT RAISE(ABORT, 'Immutable submission'); END;
                CREATE TRIGGER IF NOT EXISTS no_submitted_evidence_update BEFORE UPDATE ON evidence
                    WHEN OLD.submitted = 1 BEGIN SELECT RAISE(ABORT, 'Immutable evidence'); END;
                CREATE TRIGGER IF NOT EXISTS no_submitted_evidence_delete BEFORE DELETE ON evidence
                    WHEN OLD.submitted = 1 BEGIN SELECT RAISE(ABORT, 'Immutable evidence'); END;
                CREATE TABLE IF NOT EXISTS submit_keys (
                    application_id TEXT NOT NULL REFERENCES applications(id),
                    key TEXT NOT NULL, expected INTEGER NOT NULL, response TEXT NOT NULL,
                    PRIMARY KEY (application_id, key)
                );
                CREATE TABLE IF NOT EXISTS review_requests (
                    application_id TEXT NOT NULL REFERENCES applications(id),
                    revision INTEGER NOT NULL, targets TEXT NOT NULL,
                    PRIMARY KEY (application_id,revision)
                );
                CREATE TABLE IF NOT EXISTS face_access (
                    application_id TEXT NOT NULL REFERENCES applications(id),
                    revision INTEGER NOT NULL, submission INTEGER NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    application_id TEXT NOT NULL REFERENCES applications(id),
                    revision INTEGER NOT NULL, actor TEXT NOT NULL, role TEXT NOT NULL,
                    action TEXT NOT NULL, metadata TEXT NOT NULL, at TEXT NOT NULL,
                    PRIMARY KEY (application_id, revision)
                );
                CREATE TABLE IF NOT EXISTS verification_sync_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    owner TEXT NOT NULL, status TEXT NOT NULL, entitlements TEXT NOT NULL,
                    UNIQUE (application_id, revision)
                );
                CREATE TABLE IF NOT EXISTS verification_sync_stream (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), id TEXT NOT NULL
                );
            """)
        with self.connect(write=True) as db:
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(verification_sync_events)")
            }
            if "cccd_expires_on" not in columns:
                db.execute("ALTER TABLE verification_sync_events ADD COLUMN cccd_expires_on TEXT")
            if "capability_review" not in columns:
                db.execute("ALTER TABLE verification_sync_events ADD COLUMN capability_review TEXT")
            db.execute(
                "INSERT OR IGNORE INTO verification_sync_stream VALUES (1,?)", (str(uuid4()),)
            )
            previous = db.execute(
                """SELECT a.* FROM applications a
                   WHERE a.status <> 'DRAFT' AND NOT EXISTS (
                       SELECT 1 FROM verification_sync_events e WHERE e.application_id=a.id
                   ) ORDER BY a.id"""
            ).fetchall()
            for row in previous:
                self.record_sync_event(db, row)

    def record_sync_event(self, db, row, capability_review=None):
        entitlements = []
        cccd_expires_on = None
        if row["status"] == "APPROVED":
            try:
                submitted = self.snapshot(db, row)
                confirmed = submitted.get("confirmed") or {}
                if any(w["blocksActivation"] for w in self.warnings(submitted, row["revision"])):
                    confirmed = {}
            except EkycError as exc:
                if exc.code != ErrorCode.NOT_FOUND:
                    raise
                confirmed = {}
            confirmed = canonical_details(confirmed)
            cccd_expires_on = confirmed.get("cccd_expiry_date")
            expiry = confirmed.get("expiry_date")
            if confirmed.get("no_expiry") is True and expiry is None:
                kind = "NO_EXPIRY"
            elif confirmed.get("no_expiry") is False and isinstance(expiry, str):
                kind = "HAS_EXPIRY"
            else:
                kind = "UNKNOWN"
            for license_class in sorted(set(confirmed.get("gplx_classes", []))):
                entitlements.append(
                    {"licenseClass": license_class, "kind": kind, "expiresOn": expiry}
                )
        if capability_review is not None and entitlements:
            capability_review = {**capability_review, "submissionRevision": row["submission"]}
        else:
            capability_review = None
        db.execute(
            """INSERT INTO verification_sync_events
               (application_id, revision, owner, status, entitlements,
                cccd_expires_on, capability_review)
               VALUES (?,?,?,?,?,?,?)""",
            (
                row["id"],
                row["revision"],
                row["owner"],
                row["status"],
                json.dumps(entitlements),
                cccd_expires_on,
                json.dumps(capability_review) if capability_review else None,
            ),
        )

    def sync_events(self, after, limit):
        with self.connect() as db:
            stream = db.execute("SELECT id FROM verification_sync_stream").fetchone()["id"]
            head = db.execute(
                "SELECT COALESCE(MAX(seq), 0) AS head FROM verification_sync_events"
            ).fetchone()["head"]
            rows = db.execute(
                """SELECT * FROM verification_sync_events
                   WHERE seq>? ORDER BY seq LIMIT ?""",
                (after, limit + 1),
            ).fetchall()
        return {
            "streamId": stream,
            "head": head,
            "events": [
                {
                    "seq": row["seq"],
                    "applicationId": row["application_id"],
                    "revision": row["revision"],
                    "ownerUid": row["owner"],
                    "status": row["status"],
                    "entitlements": json.loads(row["entitlements"]),
                    **({"cccdExpiresOn": row["cccd_expires_on"]} if row["cccd_expires_on"] else {}),
                    **(
                        {"capabilityReview": json.loads(row["capability_review"])}
                        if row["capability_review"]
                        else {}
                    ),
                }
                for row in rows[:limit]
            ],
            "hasMore": len(rows) > limit,
        }

    def sync_head(self):
        with self.connect() as db:
            return dict(
                db.execute(
                    """SELECT s.id AS streamId, COALESCE(MAX(e.seq), 0) AS head
                       FROM verification_sync_stream s LEFT JOIN verification_sync_events e
                       ON 1=1 GROUP BY s.id"""
                ).fetchone()
            )

    @contextmanager
    def connect(self, *, write=False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA secure_delete = ON")
        try:
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield db
        finally:
            db.close()

    @staticmethod
    def row(db, key, actor, *, review=False):
        if review and not actor.admin:
            raise EkycError(ErrorCode.FORBIDDEN)
        row = db.execute("SELECT * FROM applications WHERE id=?", (key,)).fetchone()
        if row is None or (not review and row["owner"] != actor.uid):
            raise EkycError(ErrorCode.NOT_FOUND)
        if review and row["submission"] is None:
            raise EkycError(ErrorCode.NOT_FOUND)
        return row

    @staticmethod
    def check_revision(row, expected, *, editable=True):
        if row["revision"] != expected:
            raise EkycError(ErrorCode.REVISION_CONFLICT)
        if editable and row["status"] not in EDITABLE:
            raise EkycError(ErrorCode.INVALID_TRANSITION)

    @staticmethod
    def metadata(row):
        return {
            "id": row["id"],
            "status": row["status"],
            "revision": row["revision"],
            "submissionRevision": row["submission"],
            "reason": row["reason"],
            "updatedAt": row["updated_at"],
        }

    def warnings(self, payload, revision, *, face_statuses=(), face_quality=None):
        return verification_warnings(
            payload,
            revision,
            self.license_classes,
            face_statuses=face_statuses,
            face_quality=face_quality,
        )

    def face_warnings(self, revision, *, statuses=(), quality=None):
        return [
            warning
            for warning in self.warnings({}, revision, face_statuses=statuses, face_quality=quality)
            if warning["evidenceSource"] == "FACE_COMPARISON"
        ]

    def with_warnings(self, row, payload):
        return {
            **self.metadata(row),
            "verificationWarnings": self.warnings(payload, row["revision"]),
        }

    @staticmethod
    def snapshot(db, row, revision=None):
        key = row["id"]
        revision = row["submission"] if revision is None else revision
        record = db.execute(
            "SELECT payload FROM submissions WHERE application_id=? AND revision=?", (key, revision)
        ).fetchone()
        if record is None:
            raise EkycError(ErrorCode.NOT_FOUND)
        return json.loads(record["payload"])

    @staticmethod
    def audit(db, row, actor, action, metadata, *, review=False):
        db.execute(
            "INSERT INTO audit VALUES (?,?,?,?,?,?,?)",
            (
                row["id"],
                row["revision"],
                actor.uid,
                "admin" if review else "driver",
                action,
                json.dumps(metadata),
                now(),
            ),
        )

    def advance(
        self, db, row, payload, *, status=None, reason=None, submission=None, capability_review=None
    ):
        db.execute(
            """UPDATE applications
               SET revision=revision+1, payload=?, status=?, reason=?, submission=?, updated_at=?
               WHERE id=?""",
            (
                json.dumps(payload),
                status or row["status"],
                reason if status else row["reason"],
                submission if submission is not None else row["submission"],
                now(),
                row["id"],
            ),
        )
        updated = db.execute("SELECT * FROM applications WHERE id=?", (row["id"],)).fetchone()
        if status is not None and status != row["status"]:
            self.record_sync_event(db, updated, capability_review)
        return updated

    def create(self, actor: Actor):
        with self.connect(write=True) as db:
            row = db.execute("SELECT * FROM applications WHERE owner=?", (actor.uid,)).fetchone()
            if row is None:
                key = str(uuid4())
                payload = {"captures": {}, "proposals": {}, "confirmed": None}
                db.execute(
                    "INSERT INTO applications VALUES (?,?,'DRAFT',0,?,NULL,NULL,?)",
                    (key, actor.uid, json.dumps(payload), now()),
                )
                row = self.row(db, key, actor)
                self.audit(db, row, actor, "CREATE", {})
            return self.metadata(row)

    def list(self, actor: Actor, limit: int, offset: int, *, review=False):
        if review and not actor.admin:
            raise EkycError(ErrorCode.FORBIDDEN)
        where, args = ("submission IS NOT NULL", ()) if review else ("owner=?", (actor.uid,))
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT id, status, revision, submission, reason, updated_at FROM applications
                    WHERE {where} ORDER BY updated_at DESC, id LIMIT ? OFFSET ?""",
                (*args, limit, offset),
            )
            return [self.metadata(row) for row in rows]

    def review_queue(self, actor, limit, offset, status=None, key=None):
        if not actor.admin:
            raise EkycError(ErrorCode.FORBIDDEN)
        with self.connect() as db:
            rows = db.execute(
                """SELECT a.id,a.status,a.revision,a.submission, a.updated_at,
                          json_extract(s.payload,'$.submittedAt') AS submitted_at,
                          coalesce(json_extract(s.payload,'$.warningCount'),
                                   json_array_length(s.payload,'$.warningCodes'),0) AS warnings,
                          (SELECT count(*)-1 FROM submissions WHERE application_id=a.id) AS retries
                   FROM applications a JOIN submissions s
                     ON s.application_id=a.id AND s.revision=a.submission
                   WHERE (? IS NULL OR a.status=?) AND (? IS NULL OR a.id=?)
                   ORDER BY CASE WHEN a.status IN ('SUBMITTED','UNDER_REVIEW') THEN 0 ELSE 1 END,
                            warnings DESC, submitted_at ASC, a.id
                   LIMIT ? OFFSET ?""",
                (status, status, key, key, limit + 1, offset),
            ).fetchall()
            return {
                "items": [
                    {
                        "id": row["id"],
                        "status": row["status"],
                        "revision": row["revision"],
                        "submissionRevision": row["submission"],
                        "submittedAt": row["submitted_at"],
                        "warningCount": row["warnings"],
                        "resubmissionCount": row["retries"],
                    }
                    for row in rows[:limit]
                ],
                "hasMore": len(rows) > limit,
            }

    @staticmethod
    def requested(db, row):
        if row["status"] != "NEEDS_MORE_INFO":
            return None
        record = db.execute(
            """SELECT targets FROM review_requests WHERE application_id=?
               ORDER BY revision DESC LIMIT 1""",
            (row["id"],),
        ).fetchone()
        return json.loads(record["targets"]) if record else None

    def enforce_slot(self, db, row, slot):
        targets = self.requested(db, row)
        if targets is not None and slot not in targets["captures"]:
            raise EkycError(ErrorCode.FORBIDDEN)

    def detail(self, key, actor, *, review=False, snapshot_revision=None):
        with self.connect() as db:
            row = self.row(db, key, actor, review=review)
            data = self.metadata(row)
            data["requestedChanges"] = self.requested(db, row)
            if review:
                selected = snapshot_revision or row["submission"]
                history = db.execute(
                    """SELECT revision,json_extract(payload,'$.submittedAt') AS submittedAt
                       FROM submissions WHERE application_id=? ORDER BY revision DESC""",
                    (key,),
                ).fetchall()
                data["submissions"] = [dict(item) for item in history]
                previous = next(
                    (item["revision"] for item in history if item["revision"] < selected), None
                )
                if previous is not None:
                    prior = self.snapshot(db, row, previous)["confirmed"]
                    latest = self.snapshot(db, row, selected)["confirmed"]
                    data["editedFields"] = sorted(
                        field for field in latest if latest[field] != prior.get(field)
                    )
            if review or snapshot_revision is not None:
                data["document"] = self.snapshot(db, row, snapshot_revision)
            else:
                data["document"] = json.loads(row["payload"])
            selected = snapshot_revision or row["submission"]
            face = None
            if self.face_enabled and selected and (review or row["status"] not in EDITABLE):
                face = db.execute(
                    "SELECT statuses FROM verification_face_status "
                    "WHERE application_id=? AND submission=?",
                    (key, selected),
                ).fetchone()
            warning_payload = data["document"]
            if (
                selected
                and not review
                and snapshot_revision is None
                and row["status"] not in EDITABLE
            ):
                warning_payload = self.snapshot(db, row)
            warning_revision = warning_payload.get("revision", row["revision"])
            evidence = json.loads(face["statuses"]) if face else {}
            if isinstance(evidence, list):
                evidence = {"pairs": evidence}
            data["verificationWarnings"] = self.warnings(
                warning_payload,
                warning_revision,
                face_statuses=evidence.get("pairs", ()),
                face_quality=evidence.get("quality"),
            )
            events = db.execute(
                """SELECT revision, actor, role, action, metadata, at FROM audit
                   WHERE application_id=?
                   AND (? IS NULL OR revision<=? OR role='admin')
                   ORDER BY revision DESC LIMIT 100""",
                (
                    key,
                    row["submission"] if review and row["status"] in EDITABLE else None,
                    row["submission"],
                ),
            )
            data["history"] = [
                {
                    **{k: event[k] for k in event.keys() if review or k != "actor"},
                    "metadata": json.loads(event["metadata"]),
                }
                for event in events
            ]
            return data

    def reserve_upload(self, key, actor, revision, limit, slot=None):
        with self.connect(write=True) as db:
            row = self.row(db, key, actor)
            self.check_revision(row, revision)
            if slot is not None:
                self.enforce_slot(db, row, slot)
            cutoff = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
            db.execute("DELETE FROM upload_attempts WHERE at<=?", (cutoff,))
            attempts = db.execute(
                "SELECT count(*) FROM upload_attempts WHERE owner=?", (actor.uid,)
            ).fetchone()[0]
            if attempts >= limit:
                raise EkycError(ErrorCode.RATE_LIMITED)
            db.execute("INSERT INTO upload_attempts VALUES (?,?)", (actor.uid, now()))

    def replace(self, key, actor, revision, slot, mime, data, quality=None):
        if slot not in SLOTS:
            raise EkycError(ErrorCode.INVALID_REQUEST)
        checksum = hashlib.sha256(data).hexdigest()
        published = None
        try:
            with self.connect(write=True) as db:
                row = self.row(db, key, actor)
                self.check_revision(row, revision)
                self.enforce_slot(db, row, slot)
                payload = json.loads(row["payload"])
                old = payload["captures"].get(slot)
                if old:
                    previous = db.execute(
                        """SELECT checksum,mime FROM document_objects
                           WHERE id=? AND application_id=? AND owner=? AND slot=?""",
                        (old["id"], key, actor.uid, slot),
                    ).fetchone()
                    if previous and previous["checksum"] == checksum and previous["mime"] == mime:
                        return self.with_warnings(row, payload)
                capture, object_key = str(uuid4()), uuid4().hex
                published = object_key
                self.storage.put(object_key, data)
                db.execute(
                    """INSERT INTO document_objects
                       VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)""",
                    (
                        capture,
                        key,
                        actor.uid,
                        slot,
                        revision + 1,
                        mime,
                        len(data),
                        checksum,
                        object_key,
                        now(),
                    ),
                )
                if old:
                    db.execute(
                        "UPDATE document_objects SET retired_at=? WHERE id=? AND application_id=?",
                        (now(), old["id"], key),
                    )
                payload["captures"][slot] = {"id": capture, "mime": mime, "quality": quality}
                payload["proposals"].pop(slot, None)
                payload["confirmationCurrent"] = False
                row = self.advance(db, row, payload)
                self.audit(
                    db, row, actor, "REPLACE_CAPTURE" if old else "ADD_CAPTURE", {"slot": slot}
                )
                return self.with_warnings(row, payload)
        except BaseException as exc:
            if published is not None:
                self._rollback_object(published)
            if isinstance(exc, OSError):
                raise EkycError(ErrorCode.VERIFICATION_UNAVAILABLE) from None
            raise

    def _rollback_object(self, key):
        # Consult committed metadata after rollback before deleting the object.
        try:
            with self.connect(write=True) as db:
                if not db.execute(
                    "SELECT 1 FROM document_objects WHERE object_key=?", (key,)
                ).fetchone():
                    self.storage.delete(key)
        except (OSError, sqlite3.Error):
            # A later operator-run orphan sweep can retry.
            return

    def read_evidence(self, db, row, captures, slot):
        if slot not in captures:
            raise EkycError(ErrorCode.NOT_FOUND)
        capture = captures[slot]["id"]
        record = db.execute(
            """SELECT o.*,d.object_key AS deletion FROM document_objects o
               LEFT JOIN document_deletions d ON d.object_key=o.object_key
               WHERE o.application_id=? AND o.owner=? AND o.slot=? AND o.id=?""",
            (row["id"], row["owner"], slot, capture),
        ).fetchone()
        if record is None:
            # Phase 10.3 rows remain readable from their existing database.
            legacy = db.execute(
                "SELECT mime,data FROM evidence WHERE application_id=? AND id=?",
                (row["id"], capture),
            ).fetchone()
            if legacy is None:
                raise EkycError(ErrorCode.NOT_FOUND)
            return legacy["mime"], legacy["data"]
        if record["deletion"]:
            raise EkycError(ErrorCode.DOCUMENT_REMOVED)
        try:
            data = self.storage.read(record["object_key"])
            if (
                len(data) != record["bytes"]
                or hashlib.sha256(data).hexdigest() != record["checksum"]
            ):
                raise OSError("Object integrity check failed")
        except OSError:
            raise EkycError(ErrorCode.VERIFICATION_UNAVAILABLE) from None
        return record["mime"], data

    def capture(self, key, actor, slot, *, review=False, snapshot_revision=None, expected=None):
        with self.connect() as db:
            row = self.row(db, key, actor, review=review)
            if expected is not None:
                self.check_revision(row, expected)
                self.enforce_slot(db, row, slot)
            payload = (
                self.snapshot(db, row, snapshot_revision)
                if review or snapshot_revision is not None
                else json.loads(row["payload"])
            )
            return self.read_evidence(db, row, payload["captures"], slot)

    def save(self, key, actor, revision, *, details=None, slot=None, proposal=None):
        with self.connect(write=True) as db:
            row = self.row(db, key, actor)
            self.check_revision(row, revision)
            payload = json.loads(row["payload"])
            if details is not None:
                before = payload["confirmed"] or {}
                if "cccd_expiry_date" not in details and "cccd_expiry_date" in before:
                    details["cccd_expiry_date"] = before["cccd_expiry_date"]
                changed = sorted(field for field in details if before.get(field) != details[field])
                targets = self.requested(db, row)
                if targets is not None and set(changed) - set(targets["fields"]):
                    raise EkycError(ErrorCode.FORBIDDEN)
                payload["confirmed"] = details
                payload["confirmationCurrent"] = True
                action, event = "CONFIRM_FIELDS", {"changedFields": changed}
            else:
                self.enforce_slot(db, row, slot)
                payload["proposals"][slot] = {
                    "captureId": payload["captures"][slot]["id"],
                    "result": proposal,
                }
                action, event = "EXTRACT", {"slot": slot}
            row = self.advance(db, row, payload)
            self.audit(db, row, actor, action, event)
            return self.with_warnings(row, payload)

    def submit(self, key, actor, expected, idem, face_consent=None, warning_ack_revision=None):
        with self.connect(write=True) as db:
            row = self.row(db, key, actor)
            previous = db.execute(
                "SELECT expected,response FROM submit_keys WHERE application_id=? AND key=?",
                (key, idem),
            ).fetchone()
            if previous is not None:
                saved = json.loads(previous["response"])
                if (
                    previous["expected"] != expected
                    or saved.get("faceConsentVersion") != face_consent
                    or saved.get("warningAcknowledgementRevision") != warning_ack_revision
                ):
                    raise EkycError(ErrorCode.IDEMPOTENCY_CONFLICT)
                return saved
            self.check_revision(row, expected)
            payload = json.loads(row["payload"])
            if not payload.get("confirmationCurrent") or not REQUIRED <= payload["captures"].keys():
                raise EkycError(ErrorCode.FIELD_MISSING)
            if not Details.model_validate(payload["confirmed"]).valid():
                raise EkycError(ErrorCode.INVALID_FIELD_FORMAT)
            targets = self.requested(db, row)
            if targets is not None:
                previous = self.snapshot(db, row)["captures"]
                if any(
                    payload["captures"].get(slot) is None
                    or payload["captures"][slot]["id"] == previous.get(slot, {}).get("id")
                    for slot in targets["captures"]
                ):
                    raise EkycError(ErrorCode.FIELD_MISSING)
            warnings = comparison_warnings(payload["confirmed"])
            structured = self.warnings(payload, expected)
            requires_review = sorted({w["code"] for w in structured if w["requiresReview"]})
            if any(w["blocksSubmission"] for w in structured):
                raise EkycError(ErrorCode.FIELD_MISSING)
            if (
                requires_review or warning_ack_revision is not None
            ) and warning_ack_revision != expected:
                raise EkycError(ErrorCode.WARNING_ACK_REQUIRED)
            acknowledgement = (
                {"revision": expected, "codes": requires_review}
                if warning_ack_revision is not None
                else None
            )
            revision = expected + 1
            resubmitting = row["status"] == "NEEDS_MORE_INFO"
            consent = {"version": face_consent, "acceptedAt": now()} if face_consent else None
            db.execute(
                "INSERT INTO submissions VALUES (?,?,?)",
                (
                    key,
                    revision,
                    json.dumps(
                        {
                            **payload,
                            "revision": revision,
                            "submittedAt": now(),
                            "warningCodes": warnings,
                            "warningCount": len(requires_review),
                            "verificationWarnings": structured,
                            "warningAcknowledgement": acknowledgement,
                            **({"faceConsent": consent} if consent else {}),
                        }
                    ),
                ),
            )
            for capture in payload["captures"].values():
                db.execute(
                    """UPDATE document_objects SET first_submitted_revision=?
                       WHERE id=? AND application_id=? AND first_submitted_revision IS NULL""",
                    (revision, capture["id"], key),
                )
                db.execute(
                    "UPDATE evidence SET submitted=1 WHERE id=? AND submitted=0", (capture["id"],)
                )
            row = self.advance(db, row, payload, status="SUBMITTED", submission=revision)
            self.audit(
                db,
                row,
                actor,
                "RESUBMIT" if resubmitting else "SUBMIT",
                {"warningAcknowledgement": acknowledgement} if acknowledgement else {},
            )
            result = self.metadata(row)
            if warning_ack_revision is not None:
                result["warningAcknowledgementRevision"] = warning_ack_revision
            if consent:
                result["faceConsentVersion"] = face_consent
            db.execute(
                "INSERT INTO submit_keys VALUES (?,?,?,?)",
                (key, idem, expected, json.dumps(result)),
            )
            return result

    def draft_face_inputs(self, key, actor, revision, *, read=False):
        with self.connect(write=read) as db:
            row = self.row(db, key, actor)
            self.check_revision(row, revision)
            images = {}
            if read:
                payload = json.loads(row["payload"])
                for slot in FACE_SLOTS:
                    try:
                        _, images[slot] = self.read_evidence(db, row, payload["captures"], slot)
                    except EkycError as exc:
                        if exc.code not in (ErrorCode.NOT_FOUND, ErrorCode.DOCUMENT_REMOVED):
                            raise
                db.execute(
                    "INSERT INTO face_access VALUES (?,?,0,?,'REQUEST_DRAFT_QUALITY',?)",
                    (key, revision, actor.uid, now()),
                )
            return images

    def face_inputs(
        self,
        key,
        actor,
        revision,
        submission,
        *,
        read=False,
        audit=False,
        statuses=None,
        quality=None,
    ):
        with self.connect(write=audit or statuses is not None) as db:
            row = self.row(db, key, actor, review=True)
            self.check_revision(row, revision, editable=False)
            if submission != row["submission"]:
                raise EkycError(ErrorCode.REVISION_CONFLICT)
            if row["status"] not in {"SUBMITTED", "UNDER_REVIEW"}:
                raise EkycError(ErrorCode.INVALID_TRANSITION)
            if row["owner"] == actor.uid:
                raise EkycError(ErrorCode.FORBIDDEN)
            snapshot = self.snapshot(db, row)
            consent = snapshot.get("faceConsent", {}).get("version") == CONSENT_VERSION
            images = {}
            if consent and read:
                for slot in FACE_SLOTS:
                    try:
                        _, images[slot] = self.read_evidence(db, row, snapshot["captures"], slot)
                    except EkycError as exc:
                        if exc.code not in (ErrorCode.NOT_FOUND, ErrorCode.DOCUMENT_REMOVED):
                            raise
            if audit and consent:
                db.execute(
                    "INSERT INTO face_access VALUES (?,?,?,?,'REQUEST_COMPARISON',?)",
                    (key, revision, submission, actor.uid, now()),
                )
            if consent and statuses is not None:
                db.execute(
                    "INSERT OR REPLACE INTO verification_face_status VALUES (?,?,?)",
                    (
                        key,
                        submission,
                        json.dumps(
                            {
                                "pairs": sorted(set(statuses)),
                                "quality": {
                                    slot: code
                                    for slot, code in (quality or {}).items()
                                    if slot in FACE_SLOTS and code in FACE_QUALITY_MESSAGES
                                },
                            }
                        ),
                    ),
                )
            return consent, images

    def decision(self, key, actor, expected, status, reason, targets=None, capability_review=None):
        with self.connect(write=True) as db:
            row = self.row(db, key, actor, review=True)
            if row["owner"] == actor.uid:
                raise EkycError(ErrorCode.FORBIDDEN)
            self.check_revision(row, expected, editable=False)
            if status not in TRANSITIONS.get(row["status"], set()):
                raise EkycError(ErrorCode.INVALID_TRANSITION)
            payload = json.loads(row["payload"])
            if status == "APPROVED":
                warnings = self.warnings(self.snapshot(db, row), expected)
                if any(w["blocksActivation"] for w in warnings):
                    raise EkycError(ErrorCode.VERIFICATION_ACTIVATION_BLOCKED)
            if status == "NEEDS_MORE_INFO":
                if not targets:
                    raise EkycError(ErrorCode.INVALID_REQUEST)
                db.execute(
                    "INSERT INTO review_requests VALUES (?,?,?)",
                    (key, expected + 1, json.dumps(targets)),
                )
                payload["confirmationCurrent"] = False
            row = self.advance(
                db, row, payload, status=status, reason=reason, capability_review=capability_review
            )
            self.audit(
                db,
                row,
                actor,
                status,
                {
                    "reason": reason,
                    "requestedChanges": targets,
                    **(
                        {"capabilityGroupIds": capability_review["selectedGroupIds"]}
                        if capability_review
                        else {}
                    ),
                },
                review=True,
            )
            return self.metadata(row)
