import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from firebase_admin import auth
from PIL import Image
from pydantic import SecretStr

from ekyc.main import create_app
from ekyc.schemas.document import ExtractionResult

ROOT = "/ekyc/driver-verification/v1"
SYNC_KEY = "synthetic-sync-key-for-regression-only"
DETAILS = {
    "cccd_name": "NGUYEN VAN MAU",
    "cccd_id": "000000000001",
    "cccd_dob": "1990-02-01",
    "cccd_gender": "",
    "gplx_name": "NGUYEN VAN MAU",
    "gplx_number": "000000000002",
    "gplx_dob": "1990-02-01",
    "gplx_classes": ["A1", "B"],
    "no_expiry": True,
    "expiry_date": None,
}


def headers(token="driver-a"):
    return {"Authorization": f"Bearer {token}"}


def image(color):
    buffer = io.BytesIO()
    Image.new("RGB", (80, 52), color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def verification(settings, tmp_path, monkeypatch):
    project = "synthetic-project"

    def verify(token, *, app, check_revoked):
        assert app.project_id == project and check_revoked is True
        if token == "expired":
            raise auth.ExpiredIdTokenError("test expired", None)
        if token == "revoked":
            raise auth.RevokedIdTokenError("test revoked")
        if token == "invalid":
            raise auth.InvalidIdTokenError("test bad signature")
        issuer_project = "other-project" if token == "wrong-project" else project
        return {
            "sub": token,
            "aud": issuer_project,
            "iss": f"https://securetoken.google.com/{issuer_project}",
            "driver_verification_admin": token == "admin",
        }

    monkeypatch.setattr(auth, "verify_id_token", verify)
    app = create_app(
        settings.model_copy(
            update={
                "firebase_project_id": project,
                "verification_db_path": tmp_path / "verification.sqlite3",
                "verification_sync_key": SecretStr(SYNC_KEY),
            }
        )
    )
    with TestClient(app) as client:
        yield client


def test_token_policy_and_owner_isolation(verification):
    client = verification
    assert client.get(ROOT + "/me").status_code == 401
    for token in ("expired", "wrong-project", "invalid", "revoked"):
        response = client.get(ROOT + "/me", headers=headers(token))
        assert response.status_code == 401
        assert response.json()["errorCode"] == "UNAUTHORIZED"
    a = client.post(ROOT + "/applications", headers=headers()).json()["data"]
    path = ROOT + "/applications/" + a["id"]
    assert client.get(path, headers=headers("driver-b")).status_code == 404
    response = client.put(
        path + "/confirmation",
        json={"revision": 0, "details": DETAILS},
        headers=headers("driver-b"),
    )
    assert response.status_code == 404
    assert client.get(ROOT + "/admin/applications", headers=headers()).status_code == 403
    assert client.get(ROOT + "/admin/applications", headers=headers("admin")).json()["data"] == []
    assert client.get(path, headers=headers()).headers["cache-control"] == "no-store"


def test_concurrent_revisions_only_one_write(verification):
    client = verification
    app = client.post(ROOT + "/applications", headers=headers()).json()["data"]
    path = ROOT + "/applications/" + app["id"] + "/confirmation"

    def confirm(name):
        return client.put(
            path, headers=headers(), json={"revision": 0, "details": {**DETAILS, "gplx_name": name}}
        )

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(confirm, ("SYNTHETIC A", "SYNTHETIC B")))
    assert sorted(result.status_code for result in results) == [200, 409]
    assert (
        next(r for r in results if r.status_code == 409).json()["errorCode"] == "REVISION_CONFLICT"
    )


@pytest.mark.parametrize("final_status", ["APPROVED", "REJECTED"])
def test_submit_review_and_resubmit_keep_snapshots(verification, final_status):
    client = verification
    app = client.post(ROOT + "/applications", headers=headers()).json()["data"]
    path = ROOT + "/applications/" + app["id"]
    admin_path = ROOT + "/admin/applications/" + app["id"]
    driver, admin = headers(), headers("admin")

    def upload(slot, revision, color):
        return client.put(
            f"{path}/captures/{slot}?revision={revision}",
            content=image(color),
            headers={**driver, "Content-Type": "image/png"},
        )

    def confirm(revision, details):
        return client.put(
            path + "/confirmation",
            headers=driver,
            json={
                "revision": revision,
                "details": details,
            },
        )

    def submit(revision, idem, *, ack=True):
        return client.post(
            path + "/submit",
            headers={**driver, "Idempotency-Key": idem},
            json={
                "revision": revision,
                **({"warningAcknowledgementRevision": revision} if ack else {}),
            },
        )

    def decision(revision, status, actor=admin):
        return client.post(
            admin_path + "/decision",
            headers=actor,
            json={
                "revision": revision,
                "status": status,
                "reason": "Synthetic review",
                **(
                    {
                        "capabilityReview": {
                            "policyVersion": "VN_36_2024_108_2026_MVP_1",
                            "issuedOn": "2025-06-20",
                            "restriction": "VERIFIED_NONE",
                            "selectedGroupIds": ["moto-50-125"],
                        }
                    }
                    if status == "APPROVED"
                    else {}
                ),
                **(
                    {"captures": ["cccd_front"], "fields": ["gplx_name"]}
                    if status == "NEEDS_MORE_INFO"
                    else {}
                ),
            },
        )

    assert not submit(0, "premature").json()["success"]
    assert decision(0, "UNDER_REVIEW").status_code == 404
    for revision, slot in enumerate(("cccd_front", "cccd_back", "gplx_front", "selfie")):
        assert upload(slot, revision, "white").status_code == 200

    class FakeWorker:
        def extract(self, data, slot):
            assert data == image("white") and slot == "cccd_front"
            return ExtractionResult(slot=slot, ocr_status="OCR_NO_FIELDS")

    client.app.state.cccd_worker = FakeWorker()
    result = client.post(path + "/extract/cccd_front", headers=driver, json={"revision": 4})
    client.app.state.cccd_worker = None
    assert result.json()["data"]["proposal"]["ocrStatus"] == "OCR_NO_FIELDS"

    assert confirm(5, DETAILS).json()["success"]
    assert submit(6, "without-ack", ack=False).json()["errorCode"] == "WARNING_ACK_REQUIRED"
    stale = client.post(
        path + "/submit",
        headers={**driver, "Idempotency-Key": "stale-ack"},
        json={"revision": 6, "warningAcknowledgementRevision": 5},
    )
    assert stale.json()["errorCode"] == "WARNING_ACK_REQUIRED"
    first = submit(6, "submit-one")
    assert first.json()["data"]["status"] == "SUBMITTED"
    assert submit(6, "submit-one").json()["data"] == first.json()["data"]
    assert submit(7, "submit-one").json()["errorCode"] == "IDEMPOTENCY_CONFLICT"
    assert decision(7, "UNDER_REVIEW", driver).status_code == 403
    assert decision(7, "APPROVED").json()["errorCode"] == "INVALID_TRANSITION"
    assert decision(7, "UNDER_REVIEW").json()["data"]["revision"] == 8
    assert decision(7, "APPROVED").json()["errorCode"] == "REVISION_CONFLICT"
    assert confirm(8, DETAILS).json()["errorCode"] == "INVALID_TRANSITION"
    assert (
        client.post(
            admin_path + "/decision",
            headers=admin,
            json={
                "revision": 8,
                "status": "NEEDS_MORE_INFO",
                "reason": "Synthetic review",
            },
        ).json()["errorCode"]
        == "INVALID_REQUEST"
    )
    assert decision(8, "NEEDS_MORE_INFO").json()["data"]["revision"] == 9
    targeted = client.get(path, headers=driver).json()["data"]
    assert targeted["requestedChanges"] == {"captures": ["cccd_front"], "fields": ["gplx_name"]}
    assert upload("selfie", 9, "blue").status_code == 403
    assert confirm(9, {**DETAILS, "cccd_name": "UNREQUESTED"}).status_code == 403
    assert upload("cccd_front", 9, "grey").json()["data"]["revision"] == 10
    assert not submit(10, "submit-two").json()["success"]
    prior = client.get(admin_path + "/captures/cccd_front", headers=admin)
    assert prior.content == image("white")
    assert confirm(10, {**DETAILS, "gplx_name": "SYNTHETIC EDIT"}).json()["success"]
    assert submit(11, "submit-two").json()["data"]["status"] == "SUBMITTED"
    original = client.get(admin_path + "?snapshot=7", headers=admin).json()["data"]["document"]
    assert original["confirmed"] == DETAILS
    assert original["warningAcknowledgement"]["revision"] == 6
    assert "OCR_INCOMPLETE" in original["warningAcknowledgement"]["codes"]
    assert all(item["documentRevision"] == 6 for item in original["verificationWarnings"])
    assert "cccd_front" in original["proposals"]
    current = client.get(admin_path, headers=admin).json()["data"]["document"]
    assert current["confirmed"]["gplx_name"] == "SYNTHETIC EDIT"
    assert "cccd_front" not in current["proposals"]
    assert decision(12, "UNDER_REVIEW").json()["data"]["status"] == "UNDER_REVIEW"
    queue_url = ROOT + "/admin/review-queue?status=UNDER_REVIEW&application_id=" + app["id"]
    queue = client.get(queue_url, headers=admin).json()["data"]
    assert not queue["hasMore"] and queue["items"][0]["resubmissionCount"] == 1
    assert queue["items"][0]["warningCount"] > 0
    assert client.get(queue_url, headers=driver).status_code == 403

    with ThreadPoolExecutor(2) as pool:
        decisions = list(
            pool.map(lambda status: decision(13, status), (final_status, final_status))
        )
    assert sorted(response.status_code for response in decisions) == [200, 409]
    assert next(item for item in decisions if item.status_code == 200).json()["data"]["status"] == (
        final_status
    )
    assert next(item for item in decisions if item.status_code == 409).json()["errorCode"] == (
        "REVISION_CONFLICT"
    )
    assert submit(6, "submit-one").json()["data"] == first.json()["data"]
    feed_url = ROOT + "/internal/decision-events"
    assert client.get(feed_url, headers=admin).status_code == 401
    sync_headers = {"X-Verification-Sync-Key": SYNC_KEY}
    feed = client.get(feed_url, headers=sync_headers).json()["data"]
    events = feed["events"]
    assert {item["status"] for item in events} >= {"SUBMITTED", "UNDER_REVIEW", "NEEDS_MORE_INFO"}
    assert events[-1]["status"] == final_status
    assert events[-1]["revision"] == 14
    assert "confirmed" not in events[-1]
    assert "reason" not in events[-1]
    if final_status == "APPROVED":
        assert events[-1]["capabilityReview"]["submissionRevision"] == 12
        assert events[-1]["capabilityReview"]["selectedGroupIds"] == ["moto-50-125"]
        assert "capabilityReview" not in events[-2]
        assert events[-1]["entitlements"] == [
            {"licenseClass": h, "kind": "NO_EXPIRY", "expiresOn": None} for h in ("A1", "B")
        ]
        assert decision(14, "REVOKED", driver).status_code == 403
        assert decision(14, "REVOKED").json()["data"]["status"] == "REVOKED"
        assert decision(14, "REVOKED").status_code == 409
        replay = client.get(feed_url, params={"after": feed["head"]}, headers=sync_headers)
        revoked = replay.json()["data"]["events"]
        assert len(revoked) == 1 and revoked[0]["status"] == "REVOKED"
        assert revoked[0]["entitlements"] == []
        assert decision(15, "NEEDS_MORE_INFO").json()["data"]["status"] == "NEEDS_MORE_INFO"
    head = client.get(ROOT + "/internal/decision-head", headers=sync_headers).json()["data"]
    assert head["streamId"] == feed["streamId"]
    assert head["head"] >= feed["head"]


def test_private_upload_checks_type_ownership_rate_and_revision(verification, caplog):
    client = verification
    app = client.post(ROOT + "/applications", headers=headers()).json()["data"]
    path = ROOT + "/applications/" + app["id"] + "/captures/selfie"
    png_headers = {**headers(), "Content-Type": "image/png"}
    sample = image("blue")
    secret = b"synthetic-image-content-for-log-test"
    assert client.get(path).status_code == 401
    error = client.put(path + "?revision=0", headers=png_headers, content=secret)
    assert error.json()["errorCode"] == "INVALID_IMAGE"
    multipart = client.put(
        path + "?revision=0", headers=headers(), files={"file": ("test.png", sample, "image/png")}
    )
    assert multipart.json()["errorCode"] == "INVALID_IMAGE"
    assert client.put(path + "?revision=0", headers=png_headers, content=sample).json()["success"]
    assert client.get(path, headers=headers("driver-b")).status_code == 404
    assert client.get(path, headers=headers()).content == sample
    assert client.get(path, headers=headers()).headers["cache-control"] == "no-store"
    stale = client.put(path + "?revision=0", headers=png_headers, content=sample)
    assert stale.status_code == 409
    duplicate = client.put(path + "?revision=1", headers=png_headers, content=sample)
    assert duplicate.json()["data"]["revision"] == 1
    client.app.state.settings = client.app.state.settings.model_copy(
        update={"verification_uploads_per_minute": 1}
    )
    limited = client.put(path + "?revision=1", headers=png_headers, content=sample)
    assert limited.status_code == 429
    assert secret.decode() not in caplog.text


def test_production_verification_requires_https(settings):
    with TestClient(create_app(settings.model_copy(update={"env": "prod"}))) as client:
        assert client.get(ROOT + "/me").status_code == 403
        assert client.get("https://testserver" + ROOT + "/me").status_code == 401
        assert client.get("/health").status_code == 200


def test_admin_cors_allows_only_configured_origin(settings):
    allowed = "https://admin.example.invalid"
    config = settings.model_copy(update={"verification_admin_origins": [allowed]})
    with TestClient(create_app(config)) as client:
        for origin, status in ((allowed, 200), ("https://other.example.invalid", 400)):
            response = client.options(
                ROOT + "/admin/review-queue",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "Authorization",
                },
            )
            assert response.status_code == status
            assert response.headers.get("access-control-allow-origin") == (
                allowed if status == 200 else None
            )


def test_face_comparison_requires_admin_current_revision_and_explicit_consent(
    verification, tmp_path
):
    from ekyc.services.driver_auth import Actor
    from ekyc.services.face_comparison import CONSENT_VERSION, FaceResult, PairResult

    client = verification
    store = client.app.state.verification_store

    def seed(uid, consent):
        actor = Actor(uid, False)
        key = store.create(actor)["id"]
        for index, slot in enumerate(("cccd_front", "cccd_back", "gplx_front", "selfie")):
            store.replace(key, actor, index, slot, "image/png", image("white"))
        store.save(key, actor, 4, details=DETAILS)
        response = client.post(
            ROOT + "/applications/" + key + "/submit",
            headers={**headers(uid), "Idempotency-Key": "synthetic-key"},
            json={"revision": 5, "faceConsentVersion": consent},
        )
        return response.json()["data"]

    def face(app, token="admin", revision=6):
        path = ROOT + "/admin/applications/" + app["id"] + "/face-comparison"
        return client.post(
            path,
            headers=headers(token),
            json={
                "revision": revision,
                "submissionRevision": app["submissionRevision"],
            },
        )

    unconsented = seed("driver-a", None)
    assert face(unconsented).json()["data"]["reason"] == "DISABLED"

    class Worker:
        calls = 0

        def compare_faces(self, inputs):
            self.calls += 1
            assert set(inputs) == {"cccd_front", "gplx_front", "selfie"}
            return FaceResult(
                mode="EXPERIMENTAL",
                reason="synthetic",
                quality={"gplx_front": "FACE_BLURRY", "selfie": "READY"},
                pairs=[
                    PairResult(left="gplx_front", right="selfie", status="INSUFFICIENT_QUALITY")
                ],
            )

    worker = Worker()
    manifest = tmp_path / "models.json"
    manifest.touch()
    client.app.state.settings = client.app.state.settings.model_copy(
        update={
            "face_comparison_enabled": True,
            "face_comparison_manifest": manifest,
            "ocr_backend": "easyocr",
        }
    )
    store.face_enabled = True
    try:
        client.app.state.cccd_worker = worker
        assert face(unconsented, token="driver-a").status_code == 403
        assert face(unconsented).json()["data"]["reason"] == "CONSENT_MISSING"
        assert worker.calls == 0
        consented = seed("driver-b", CONSENT_VERSION)
        assert face(consented, revision=5).status_code == 409
        result = face(consented).json()["data"]
        assert result["liveness"] == "NOT_EVALUATED"
        warning = result["verificationWarnings"][0]
        assert warning["code"] == "FACE_INSUFFICIENT_QUALITY"
        assert warning["slot"] == "gplx_front"
        assert not warning["blocksActivation"] and not warning["blocksSubmission"]
        owner_result = client.get(
            ROOT + "/applications/" + consented["id"], headers=headers("driver-b")
        ).json()["data"]
        assert warning in owner_result["verificationWarnings"]
        assert worker.calls == 1
        with store.connect() as db:
            audit = db.execute("SELECT action, revision, submission FROM face_access").fetchall()
            assert [tuple(row) for row in audit] == [("REQUEST_COMPARISON", 6, 6)]
            snapshot = db.execute(
                "SELECT payload FROM submissions WHERE application_id=?", (consented["id"],)
            ).fetchone()["payload"]
            assert "faceConsent" in snapshot and "synthetic" not in snapshot
            retained = db.execute("SELECT statuses FROM verification_face_status").fetchone()[0]
            assert json.loads(retained) == {
                "pairs": ["INSUFFICIENT_QUALITY"],
                "quality": {"gplx_front": "FACE_BLURRY"},
            }
        mismatch = client.post(
            ROOT + "/applications/" + consented["id"] + "/submit",
            headers={**headers("driver-b"), "Idempotency-Key": "synthetic-key"},
            json={"revision": 5},
        )
        assert mismatch.json()["errorCode"] == "IDEMPOTENCY_CONFLICT"
    finally:
        client.app.state.cccd_worker = None


def test_expired_document_allows_submission_but_blocks_approval(verification):
    from ekyc.services.driver_auth import Actor

    client = verification
    store = client.app.state.verification_store
    driver = Actor("driver-a", False)
    key = store.create(driver)["id"]
    for revision, slot in enumerate(("cccd_front", "cccd_back", "gplx_front", "selfie")):
        store.replace(key, driver, revision, slot, "image/png", image("white"))
    path = ROOT + "/applications/" + key
    admin = ROOT + "/admin/applications/" + key
    confirm = client.put(
        path + "/confirmation",
        headers=headers(),
        json={"revision": 4, "details": {**DETAILS, "cccd_expiry_date": "2000-01-01"}},
    ).json()["data"]
    expiry = next(w for w in confirm["verificationWarnings"] if w["code"] == "CCCD_EXPIRED")
    assert expiry["blocksActivation"] and not expiry["blocksSubmission"]
    assert (
        client.post(
            path + "/submit",
            headers={**headers(), "Idempotency-Key": "expired-test"},
            json={"revision": 5, "warningAcknowledgementRevision": 5},
        ).json()["data"]["status"]
        == "SUBMITTED"
    )
    assert (
        client.post(
            admin + "/decision",
            headers=headers("admin"),
            json={"revision": 6, "status": "UNDER_REVIEW"},
        ).status_code
        == 200
    )
    result = client.post(
        admin + "/decision", headers=headers("admin"), json={"revision": 7, "status": "APPROVED"}
    )
    assert result.status_code == 409
    assert result.json()["errorCode"] == "VERIFICATION_ACTIVATION_BLOCKED"
    review = client.get(admin, headers=headers("admin")).json()["data"]
    assert review["status"] == "UNDER_REVIEW"
    assert (
        review["verificationWarnings"]
        == client.get(path, headers=headers()).json()["data"]["verificationWarnings"]
    )


def test_draft_face_quality_is_opt_in_ephemeral_and_revision_bound(verification, tmp_path):
    from ekyc.services.driver_auth import Actor
    from ekyc.services.face_comparison import CONSENT_VERSION, FaceResult

    client = verification
    store = client.app.state.verification_store
    owner = Actor("driver-a", False)
    key = store.create(owner)["id"]
    for revision, slot in enumerate(("cccd_front", "gplx_front", "selfie")):
        store.replace(key, owner, revision, slot, "image/png", image("white"))
    route = ROOT + "/applications/" + key + "/face-quality"
    body = {"revision": 3, "faceConsentVersion": CONSENT_VERSION}
    assert client.post(route, headers=headers(), json={"revision": 3}).status_code == 400
    assert client.post(route, headers=headers("driver-b"), json=body).status_code == 404
    response = client.post(route, headers=headers(), json=body)
    assert response.json()["data"]["status"] == "NOT_EVALUATED"
    assert response.headers["Cache-Control"] == "no-store"
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM face_access").fetchone()[0] == 0

    class Worker:
        change = False

        def check_face_quality(self, inputs):
            assert set(inputs) == {"cccd_front", "gplx_front", "selfie"}
            if self.change:
                store.replace(key, owner, 3, "selfie", "image/png", image("black"))
            return FaceResult(reason="QUALITY_ONLY", quality={"selfie": "FACE_GLARE"})

    manifest = tmp_path / "manifest.json"
    manifest.touch()
    client.app.state.settings = client.app.state.settings.model_copy(
        update={
            "face_comparison_enabled": True,
            "face_comparison_manifest": manifest,
            "ocr_backend": "easyocr",
        }
    )
    worker = Worker()
    try:
        client.app.state.cccd_worker = worker
        result = client.post(route, headers=headers(), json=body).json()["data"]
        assert result["status"] == "QUALITY_CHECKED"
        assert result["revision"] == 3
        assert set(result) == {"status", "revision", "verificationWarnings"}
        warning = next(
            w for w in result["verificationWarnings"] if w["code"] == "FACE_INSUFFICIENT_QUALITY"
        )
        assert warning["slot"] == "selfie" and not warning["blocksSubmission"]
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM verification_face_status").fetchone()[0] == 0
        worker.change = True
        stale = client.post(route, headers=headers(), json=body)
        assert stale.status_code == 409
    finally:
        client.app.state.cccd_worker = None
