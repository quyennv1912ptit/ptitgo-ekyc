import asyncio
from datetime import date
from secrets import compare_digest
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.pipeline.image_loader import open_checked_image
from ekyc.schemas.common import ApiResponse
from ekyc.schemas.document import DocumentSlot
from ekyc.services.driver_auth import Actor, authenticated_actor
from ekyc.services.driver_domain import Details
from ekyc.services.face_comparison import CONSENT_VERSION, FaceResult, enabled
from ekyc.services.verification_store import VerificationStore

router = APIRouter(prefix="/ekyc/driver-verification/v1", tags=["driver-verification"])
User = Annotated[Actor, Depends(authenticated_actor)]
CaptureSlot = Literal["cccd_front", "cccd_back", "gplx_front", "gplx_back", "selfie"]


def store(request: Request) -> VerificationStore:
    value = request.app.state.verification_store
    if value is None:
        raise EkycError(ErrorCode.VERIFICATION_UNAVAILABLE)
    return value


Store = Annotated[VerificationStore, Depends(store)]
Revision = Annotated[int, Query(ge=0)]
Snapshot = Annotated[int | None, Query(ge=1)]


class Version(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(ge=0, strict=True)


class Submission(Version):
    warningAcknowledgementRevision: int | None = Field(default=None, ge=0, strict=True)
    faceConsentVersion: Literal["face-comparison-v1"] | None = None


class FaceQualityReview(Version):
    faceConsentVersion: Literal["face-comparison-v1"]


class FaceReview(Version):
    submissionRevision: int = Field(ge=1, strict=True)


class Confirmation(Version):
    details: Details


class QrScanRequest(Version):
    rawPayload: str = Field(min_length=1, max_length=1024)
    documentSide: Literal["FRONT", "BACK", "UNKNOWN"] = "UNKNOWN"
    documentType: Literal["CCCD", "GPLX"] = "CCCD"
    attachmentId: UUID | None = None


class CapabilityReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policyVersion: str = Field(pattern=r"^[A-Z0-9_]{1,80}$")
    issuedOn: date
    restriction: Literal["UNKNOWN", "VERIFIED_NONE", "AUTO_ONLY", "LEGACY_A1_LIMIT", "OTHER"]
    restrictionCode: str | None = Field(default=None, pattern=r"^[A-Z0-9.]{1,20}$")
    selectedGroupIds: list[Annotated[str, Field(pattern=r"^[a-z0-9-]{1,64}$")]] = Field(
        min_length=1, max_length=20
    )


class Decision(Version):
    status: Literal["UNDER_REVIEW", "NEEDS_MORE_INFO", "APPROVED", "REJECTED", "REVOKED"]
    reason: str | None = Field(default=None, max_length=500)
    capabilityReview: CapabilityReview | None = None
    captures: list[CaptureSlot] = Field(default_factory=list, max_length=5)
    fields: list[str] = Field(default_factory=list, max_length=12)


def ok(request: Request, result):
    return ApiResponse.ok(result, request.state.request_id)


def authorize_sync(
    request: Request,
    sync_key: Annotated[str | None, Header(alias="X-Verification-Sync-Key")] = None,
):
    expected = request.app.state.settings.verification_sync_key
    if expected is None or len(expected.get_secret_value()) < 32:
        raise EkycError(ErrorCode.NOT_FOUND)
    if sync_key is None or not compare_digest(
        expected.get_secret_value().encode(), sync_key.encode()
    ):
        raise EkycError(ErrorCode.UNAUTHORIZED)


@router.get(
    "/internal/decision-events", include_in_schema=False, dependencies=[Depends(authorize_sync)]
)
def decision_events(
    request: Request,
    db: Store,
    after: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
):
    return ok(request, db.sync_events(after, 100))


@router.get(
    "/internal/decision-head", include_in_schema=False, dependencies=[Depends(authorize_sync)]
)
def decision_head(request: Request, db: Store):
    return ok(request, db.sync_head())


@router.get("/me")
def me(request: Request, actor: User):
    result = {"uid": actor.uid, "admin": actor.admin}
    if enabled(request.app.state.settings):
        result["faceConsentVersion"] = CONSENT_VERSION
    return ok(request, result)


@router.post("/applications")
def create(request: Request, actor: User, db: Store):
    return ok(request, db.create(actor))


@router.get("/applications")
def list_mine(request: Request, actor: User, db: Store):
    return ok(request, db.list(actor, 1, 0))


@router.get("/applications/{application_id}")
def detail(
    request: Request, application_id: UUID, actor: User, db: Store, snapshot: Snapshot = None
):
    return ok(request, db.detail(str(application_id), actor, snapshot_revision=snapshot))


def checked_image(data: bytes, max_pixels: int, mime: str):
    if mime not in {"image/jpeg", "image/png", "image/webp"}:
        raise EkycError(ErrorCode.INVALID_IMAGE)
    try:
        with open_checked_image(data, max_pixels, mime=mime) as image:
            image.load()
            width, height = image.size
            return {
                "width": width,
                "height": height,
                "warnings": ["SMALL"] if min(width, height) < 480 else [],
            }
    except (OSError, SyntaxError, ValueError, OverflowError):
        raise EkycError(ErrorCode.INVALID_IMAGE) from None


@router.put("/applications/{application_id}/captures/{slot}")
async def replace(
    request: Request,
    application_id: UUID,
    slot: CaptureSlot,
    revision: Revision,
    actor: User,
    db: Store,
):
    settings = request.app.state.settings
    uploads = request.app.state.verification_uploads
    if not uploads.acquire(blocking=False):
        raise EkycError(ErrorCode.ENGINE_BUSY)
    try:
        key = str(application_id)
        await run_in_threadpool(
            db.reserve_upload, key, actor, revision, settings.verification_uploads_per_minute, slot
        )
        mime = request.headers.get("content-type", "")
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise EkycError(ErrorCode.INVALID_IMAGE)
        buffer = bytearray()
        try:
            async with asyncio.timeout(settings.verification_upload_timeout_s):
                async for chunk in request.stream():
                    if len(buffer) + len(chunk) > settings.max_image_bytes:
                        raise EkycError(ErrorCode.IMAGE_TOO_LARGE)
                    buffer.extend(chunk)
        except TimeoutError:
            raise EkycError(ErrorCode.ENGINE_TIMEOUT) from None
        except ClientDisconnect:
            raise EkycError(ErrorCode.INVALID_REQUEST) from None
        data = bytes(buffer)
        quality = await run_in_threadpool(checked_image, data, settings.max_image_pixels, mime)
        result = await run_in_threadpool(
            db.replace, key, actor, revision, slot, mime, data, quality
        )
        return ok(request, result)
    finally:
        uploads.release()


@router.post("/applications/{application_id}/extract/{slot}")
def extract(
    request: Request,
    application_id: UUID,
    slot: DocumentSlot,
    body: Version,
    actor: User,
    db: Store,
):
    key = str(application_id)
    _, data = db.capture(key, actor, slot, expected=body.revision)
    worker = request.app.state.cccd_worker
    if worker is None:
        raise EkycError(ErrorCode.VERIFICATION_UNAVAILABLE)
    proposal = worker.extract(data, slot).model_dump(by_alias=True, mode="json")
    result = db.save(key, actor, body.revision, slot=slot, proposal=proposal)
    return ok(request, {**result, "proposal": proposal})


@router.post("/applications/{application_id}/qr-scan")
def qr_scan(
    request: Request,
    application_id: UUID,
    body: QrScanRequest,
    actor: User,
    db: Store,
):
    return ok(
        request,
        db.save_qr_live_scan(
            str(application_id),
            actor,
            body.revision,
            body.rawPayload,
            document_side=body.documentSide,
            document_type=body.documentType,
            attachment_id=str(body.attachmentId) if body.attachmentId else None,
        ),
    )


@router.put("/applications/{application_id}/confirmation")
def confirm(request: Request, application_id: UUID, body: Confirmation, actor: User, db: Store):
    if not body.details.valid():
        raise EkycError(ErrorCode.INVALID_FIELD_FORMAT)
    details = body.details.model_dump(mode="json")
    if "cccd_expiry_date" not in body.details.model_fields_set:
        details.pop("cccd_expiry_date")
    return ok(request, db.save(str(application_id), actor, body.revision, details=details))


@router.post("/applications/{application_id}/submit")
def submit(
    request: Request,
    application_id: UUID,
    body: Submission,
    actor: User,
    db: Store,
    idem: Annotated[str, Header(alias="Idempotency-Key", pattern=r"^[!-~]{1,128}$")],
):
    return ok(
        request,
        db.submit(
            str(application_id),
            actor,
            body.revision,
            idem,
            body.faceConsentVersion,
            body.warningAcknowledgementRevision,
        ),
    )


@router.get("/admin/applications")
def admin_list(
    request: Request,
    actor: User,
    db: Store,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
):
    return ok(request, db.list(actor, limit, offset, review=True))


@router.get("/admin/review-queue")
def review_queue(
    request: Request,
    actor: User,
    db: Store,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    status: Literal[
        "SUBMITTED", "UNDER_REVIEW", "NEEDS_MORE_INFO", "APPROVED", "REJECTED", "REVOKED"
    ]
    | None = None,
    application_id: UUID | None = None,
):
    return ok(
        request,
        db.review_queue(
            actor, limit, offset, status, str(application_id) if application_id else None
        ),
    )


@router.post("/applications/{application_id}/face-quality")
def face_quality(
    request: Request, application_id: UUID, body: FaceQualityReview, actor: User, db: Store
):
    key = str(application_id)
    db.draft_face_inputs(key, actor, body.revision)
    result = FaceResult(reason="DISABLED")
    if enabled(request.app.state.settings):
        worker = request.app.state.cccd_worker
        result = FaceResult(reason="UNAVAILABLE")
        if worker is not None:
            jobs = request.app.state.face_jobs
            if not jobs.acquire(blocking=False):
                raise EkycError(ErrorCode.ENGINE_BUSY)
            try:
                images = db.draft_face_inputs(key, actor, body.revision, read=True)
                result = worker.check_face_quality(images)
                db.draft_face_inputs(key, actor, body.revision)
            finally:
                jobs.release()
    return ok(
        request,
        {
            "revision": body.revision,
            "status": "QUALITY_CHECKED" if result.reason == "QUALITY_ONLY" else "NOT_EVALUATED",
            "verificationWarnings": db.face_warnings(body.revision, quality=result.quality),
        },
    )


@router.get("/admin/applications/{application_id}")
def admin_detail(
    request: Request, application_id: UUID, actor: User, db: Store, snapshot: Snapshot = None
):
    detail = db.detail(str(application_id), actor, review=True, snapshot_revision=snapshot)
    detail["faceComparisonAvailable"] = enabled(request.app.state.settings)
    return ok(request, detail)


@router.post("/admin/applications/{application_id}/face-comparison")
def face_comparison(
    request: Request, application_id: UUID, body: FaceReview, actor: User, db: Store
):
    active = enabled(request.app.state.settings)
    key = str(application_id)
    consent, _ = db.face_inputs(key, actor, body.revision, body.submissionRevision)
    if not active:
        return ok(request, FaceResult(reason="DISABLED"))
    if not consent:
        return ok(request, FaceResult(reason="CONSENT_MISSING"))
    worker = request.app.state.cccd_worker
    if worker is None:
        return ok(request, FaceResult(reason="UNAVAILABLE"))
    jobs = request.app.state.face_jobs
    if not jobs.acquire(blocking=False):
        raise EkycError(ErrorCode.ENGINE_BUSY)
    try:
        _, images = db.face_inputs(
            key,
            actor,
            body.revision,
            body.submissionRevision,
            read=True,
            audit=True,
        )
        result = worker.compare_faces(images)
        statuses = [pair.status for pair in result.pairs]
        db.face_inputs(
            key,
            actor,
            body.revision,
            body.submissionRevision,
            statuses=statuses if result.mode == "EXPERIMENTAL" else None,
            quality=result.quality,
        )
        return ok(
            request,
            {
                **result.model_dump(mode="json"),
                **(
                    {
                        "verificationWarnings": db.face_warnings(
                            body.submissionRevision,
                            statuses=statuses,
                            quality=result.quality,
                        )
                    }
                    if result.mode == "EXPERIMENTAL"
                    else {}
                ),
            },
        )
    finally:
        jobs.release()


@router.post("/admin/applications/{application_id}/decision")
def decision(request: Request, application_id: UUID, body: Decision, actor: User, db: Store):
    if not actor.admin:
        raise EkycError(ErrorCode.FORBIDDEN)
    if body.status in {"NEEDS_MORE_INFO", "REJECTED", "REVOKED"} and not body.reason:
        raise EkycError(ErrorCode.INVALID_REQUEST)
    if body.capabilityReview is not None and body.status != "APPROVED":
        raise EkycError(ErrorCode.INVALID_REQUEST)
    fields = set(body.fields)
    if fields - Details.model_fields.keys():
        raise EkycError(ErrorCode.INVALID_REQUEST)
    if fields & {"expiry_date", "no_expiry"}:
        fields.update({"expiry_date", "no_expiry"})
    if body.status == "NEEDS_MORE_INFO":
        if not body.captures and not fields:
            raise EkycError(ErrorCode.INVALID_REQUEST)
        targets = {"captures": sorted(set(body.captures)), "fields": sorted(fields)}
    else:
        if body.captures or fields:
            raise EkycError(ErrorCode.INVALID_REQUEST)
        targets = None
    return ok(
        request,
        db.decision(
            str(application_id),
            actor,
            body.revision,
            body.status,
            body.reason,
            targets,
            body.capabilityReview.model_dump(mode="json") if body.capabilityReview else None,
        ),
    )


@router.get("/applications/{application_id}/captures/{slot}")
def capture(
    application_id: UUID, slot: CaptureSlot, actor: User, db: Store, snapshot: Snapshot = None
):
    mime, data = db.capture(str(application_id), actor, slot, snapshot_revision=snapshot)
    return Response(data, media_type=mime)


@router.get("/admin/applications/{application_id}/captures/{slot}")
def admin_capture(
    application_id: UUID, slot: CaptureSlot, actor: User, db: Store, snapshot: Snapshot = None
):
    mime, data = db.capture(
        str(application_id), actor, slot, review=True, snapshot_revision=snapshot
    )
    return Response(data, media_type=mime)
