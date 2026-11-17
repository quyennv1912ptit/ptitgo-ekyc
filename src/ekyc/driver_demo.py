"""Loopback-only driver workflow demo. Use synthetic data."""

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ekyc.config import Settings
from ekyc.core.errors import EkycError, ErrorCode
from ekyc.core.logging import setup_logging
from ekyc.driver_extraction import merge_observations, source_observation, suggestions
from ekyc.pipeline.image_loader import open_checked_image
from ekyc.services.cccd_worker import CccdWorker, build_demo_service
from ekyc.services.driver_domain import (
    REQUIRED as REQUIRED,
)
from ekyc.services.driver_domain import (
    SLOTS as SLOTS,
)
from ekyc.services.driver_domain import (
    Details as Details,
)
from ekyc.services.driver_domain import (
    canonical_details as canonical_details,
)
from ekyc.services.driver_domain import (
    comparison_warnings as comparison_warnings,
)
from ekyc.services.driver_domain import (
    conflict_code as conflict_code,
)
from ekyc.services.driver_domain import (
    name_comparison as name_comparison,
)

LIMIT = 8 * 1024 * 1024
EDITABLE = {"DRAFT", "NEEDS_RESUBMISSION"}
FRONTEND = Path(__file__).resolve().parents[2] / "frontend/driver-capture"


class Decision(BaseModel):
    action: str
    reason: str = Field(default="", max_length=500)
    revision: int


def now():
    return datetime.now(UTC).isoformat()


@contextmanager
def database(path):
    db = sqlite3.connect(path)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        with db:
            yield db
    finally:
        db.close()


def inspect_image(data, mime):
    if not data or len(data) > LIMIT:
        raise HTTPException(413, "Ảnh vượt giới hạn 8 MB hoặc rỗng.")
    try:
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise EkycError(ErrorCode.INVALID_IMAGE)
        with open_checked_image(data, 40_000_000, mime=mime) as img:
            width, height = img.size
            img.load()
    except EkycError as exc:
        if exc.code == ErrorCode.IMAGE_TOO_LARGE:
            raise HTTPException(413, "Ảnh vượt giới hạn kích thước.") from None
        raise HTTPException(422, "Ảnh không hợp lệ. Chọn JPEG, PNG hoặc WebP.") from None
    except (OSError, ValueError, SyntaxError, OverflowError):
        raise HTTPException(422, "Ảnh không hợp lệ. Chọn JPEG, PNG hoặc WebP.") from None
    return {
        "width": width,
        "height": height,
        "warning_codes": (["SMALL"] if min(width, height) < 480 else []),
    }


def create_app(path, tokens):
    settings = Settings(ekyc_api_key="driver-demo", max_image_bytes=LIMIT)
    if len(tokens) < 2 or any(len(token) < 24 for token in tokens):
        raise RuntimeError("Configure separate strong demo tokens.")
    path = Path(path).resolve()
    if path.is_relative_to(FRONTEND):
        raise RuntimeError("Database must be outside the frontend directory.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with database(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS applications (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
            revision INTEGER NOT NULL, created_at TEXT NOT NULL, submitted_at TEXT,
            reviewed_at TEXT, details TEXT, reason TEXT, last_key TEXT
        );
        CREATE TABLE IF NOT EXISTS captures (
            application_id TEXT NOT NULL, slot TEXT NOT NULL, mime TEXT NOT NULL,
            data BLOB NOT NULL, quality TEXT NOT NULL, PRIMARY KEY (application_id, slot)
        );
        CREATE TABLE IF NOT EXISTS history (
            application_id TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
            revision INTEGER NOT NULL, timestamp TEXT NOT NULL, reason TEXT
        );
        CREATE TABLE IF NOT EXISTS extraction_runs (
            application_id TEXT NOT NULL, slot TEXT NOT NULL, revision INTEGER NOT NULL,
            image_sha TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(application_id, slot)
        );
        CREATE TABLE IF NOT EXISTS extraction_reviews (
            application_id TEXT NOT NULL, revision INTEGER NOT NULL, result TEXT NOT NULL,
            PRIMARY KEY(application_id, revision)
        );
        PRAGMA user_version = 2;
        """)
    inference = CccdWorker(settings, build_demo_service)

    @asynccontextmanager
    async def lifespan(app):
        try:
            if settings.inference_warm_up:
                await run_in_threadpool(inference.warm_up)
            yield
        finally:
            await run_in_threadpool(inference.close)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": "Yêu cầu không hợp lệ."}, status_code=422)

    @app.middleware("http")
    async def privacy(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def authenticate(authorization: Annotated[str | None, Header()] = None):
        bearer = (authorization or "").removeprefix("Bearer ")
        for token, actor in tokens.items():
            if (
                authorization
                and authorization.startswith("Bearer ")
                and secrets.compare_digest(bearer.encode(), token.encode())
            ):
                return actor
        raise HTTPException(401, "Hãy đăng nhập bằng token demo.")

    actor_dependency = Depends(authenticate)
    extraction_lock = Lock()
    last_extraction = {}

    def extract_source(raw, slot, source):
        try:
            result = inference.extract(raw, slot, source, timeout=35 if source == "ocr" else 8)
            return source_observation(result, source)
        except EkycError as exc:
            if source == "qr":
                status = "QR_DECODE_FAILED"
            else:
                status = "OCR_TIMEOUT" if exc.code == ErrorCode.ENGINE_TIMEOUT else "OCR_FAILED"
            return {"status": status, "values": {}}

    def require(actor, role):
        if actor[1] != role:
            raise HTTPException(403, "Tài khoản không có quyền thao tác.")

    def find(db, key, actor):
        row = db.execute("SELECT * FROM applications WHERE id=?", (key,)).fetchone()
        if row is None or (actor[1] != "admin" and row["owner"] != actor[0]):
            raise HTTPException(404, "Không tìm thấy hồ sơ.")
        return row

    def public(row):
        result = {key: row[key] for key in row.keys() if key != "last_key"}
        result["details"] = (
            canonical_details(json.loads(result["details"])) if result["details"] else None
        )
        return result

    def audit(db, key, actor, action, revision, reason=None):
        db.execute(
            "INSERT INTO history VALUES (?, ?, ?, ?, ?, ?)",
            (key, actor[0], action, revision, now(), reason),
        )

    @app.get("/api/me")
    def me(actor=actor_dependency):
        return {"role": actor[1]}

    @app.get("/api/applications")
    def applications(actor=actor_dependency):
        with database(path) as db:
            if actor[1] == "admin":
                rows = db.execute("SELECT * FROM applications WHERE status='PENDING_REVIEW'")
            else:
                rows = db.execute("SELECT * FROM applications WHERE owner=?", (actor[0],))
            return [{k: r[k] for k in ("id", "status", "revision", "reason")} for r in rows]

    @app.put("/api/applications/{application_id}")
    def draft(application_id: UUID, actor=actor_dependency):
        require(actor, "driver")
        key = str(application_id)
        with database(path) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM applications WHERE id=?", (key,)).fetchone() is None:
                db.execute(
                    "INSERT INTO applications (id,owner,status,revision,created_at) "
                    "VALUES (?,?,'DRAFT',0,?)",
                    (key, actor[0], now()),
                )
                audit(db, key, actor, "DRAFT", 0)
            return public(find(db, key, actor))

    @app.get("/api/applications/{application_id}")
    def details(application_id: UUID, actor=actor_dependency):
        key = str(application_id)
        with database(path) as db:
            result = public(find(db, key, actor))
            result["captures"] = {
                r["slot"]: json.loads(r["quality"])
                for r in db.execute(
                    "SELECT slot,quality FROM captures WHERE application_id=?", (key,)
                )
            }
            result["history"] = [
                dict(r)
                for r in db.execute(
                    "SELECT actor,action,revision,timestamp,reason FROM history "
                    "WHERE application_id=? ORDER BY rowid",
                    (key,),
                )
            ]
            result["extraction_history"] = [
                {"revision": review[0], **json.loads(review[1])}
                for review in db.execute(
                    "SELECT revision,result FROM extraction_reviews WHERE application_id=? "
                    "ORDER BY revision",
                    (key,),
                )
            ]
            return result

    @app.get("/api/applications/{application_id}/captures/{slot}")
    def image(application_id: UUID, slot: str, actor=actor_dependency):
        key = str(application_id)
        with database(path) as db:
            find(db, key, actor)
            row = db.execute(
                "SELECT data,mime FROM captures WHERE application_id=? AND slot=?", (key, slot)
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Không tìm thấy ảnh.")
            return Response(row["data"], media_type=row["mime"])

    @app.post("/api/applications/{application_id}/extract/{slot}")
    async def extract(application_id: UUID, slot: str, request: Request, actor=actor_dependency):
        from starlette.concurrency import run_in_threadpool

        require(actor, "driver")
        if request.headers.get("X-Synthetic-Demo") != "confirmed":
            raise HTTPException(422, "Cần xác nhận chỉ xử lý ảnh tổng hợp.")
        key = str(application_id)
        if slot not in SLOTS - {"selfie"}:
            raise HTTPException(422, "Slot không hỗ trợ đọc tự động.")
        with database(path) as db:
            row = find(db, key, actor)
            if row["status"] not in EDITABLE:
                raise HTTPException(409, "Hồ sơ đã khóa.")
            revision = row["revision"]
        if not extraction_lock.acquire(blocking=False):
            raise HTTPException(429, "Đang xử lý ảnh. Hãy thử lại.")
        try:
            tick = time.monotonic()
            if tick - last_extraction.get(actor[0], -100) < 1:
                raise HTTPException(429, "Hãy chờ một giây trước khi đọc lại.")
            last_extraction[actor[0]] = tick
            buffer = bytearray()
            async for chunk in request.stream():
                if len(buffer) + len(chunk) > LIMIT:
                    raise HTTPException(413, "Ảnh vượt giới hạn 8 MB.")
                buffer.extend(chunk)
            raw = bytes(buffer)
            await run_in_threadpool(inspect_image, raw, request.headers.get("Content-Type"))
            if os.environ.get("DRIVER_DEMO_MODAL_ORIGIN"):
                from ekyc.services.demo_modal import extract_modal

                try:
                    extracted = await run_in_threadpool(extract_modal, raw, slot)
                except Exception:
                    raise HTTPException(502, "Không kết nối được dịch vụ đọc ảnh Modal.") from None
                qr = source_observation(extracted, "qr")
                ocr = source_observation(extracted, "ocr")
            else:
                qr = await run_in_threadpool(extract_source, raw, slot, "qr")
                ocr = await run_in_threadpool(extract_source, raw, slot, "ocr")
            result = {"qr": qr, "ocr": ocr, "extraction": merge_observations(slot, qr, ocr)}
            with database(path) as db:
                db.execute("BEGIN IMMEDIATE")
                row = find(db, key, actor)
                if row["revision"] != revision or row["status"] not in EDITABLE:
                    raise HTTPException(409, "Hồ sơ đã thay đổi.")
                db.execute(
                    "INSERT OR REPLACE INTO extraction_runs VALUES (?,?,?,?,?)",
                    (key, slot, revision, hashlib.sha256(raw).hexdigest(), json.dumps(result)),
                )
            return result
        finally:
            extraction_lock.release()

    @app.post("/api/applications/{application_id}/submit")
    async def submit(application_id: UUID, request: Request, actor=actor_dependency):
        require(actor, "driver")
        key = str(application_id)
        retry_key = request.headers.get("Idempotency-Key", "")
        if not retry_key or len(retry_key) > 100:
            raise HTTPException(400, "Thiếu mã gửi hồ sơ.")
        with database(path) as db:
            current = find(db, key, actor)
            if current["last_key"] == retry_key:
                return public(current)
            if current["status"] not in EDITABLE:
                raise HTTPException(409, "Hồ sơ không ở trạng thái có thể nộp.")
        # Bound the request before multipart parsing, even without Content-Length.
        buffer = bytearray()
        async for chunk in request.stream():
            if len(buffer) + len(chunk) > LIMIT * len(SLOTS) + 65536:
                raise HTTPException(413, "Hồ sơ vượt giới hạn dung lượng.")
            buffer.extend(chunk)
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": bytes(buffer), "more_body": False}

        bounded_request = Request(request.scope, receive)
        async with bounded_request.form(max_files=5, max_fields=1, max_part_size=16384) as form:
            try:
                payload = json.loads(form["data"])
                fields = Details.model_validate(canonical_details(payload["details"]))
                revision = payload["revision"]
                name_copy = payload.get("name_copy_confirmation")
                if name_copy is not None and (
                    not isinstance(name_copy, str)
                    or len(name_copy) > 150
                    or name_comparison(fields.cccd_name, name_copy) != "DIACRITIC_MISMATCH"
                    or fields.cccd_name != fields.gplx_name
                ):
                    raise ValueError
                remove_optional = payload.get("remove_optional", False)
                if type(revision) is not int or type(remove_optional) is not bool:
                    raise ValueError
                warnings = payload.get("warnings", {})
                if (
                    not isinstance(warnings, dict)
                    or not fields.valid()
                    or (payload["confirmed"] is not True or payload["synthetic_only"] is not True)
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                raise HTTPException(422, "Thiếu hoặc sai thông tin xác nhận hồ sơ.") from None
            updates = []
            for slot, upload in form.multi_items():
                if slot == "data":
                    continue
                if (
                    slot not in SLOTS
                    or not isinstance(upload, UploadFile)
                    or any(x[0] == slot for x in updates)
                ):
                    raise HTTPException(422, "Slot ảnh không hợp lệ.")
                raw = await upload.read(LIMIT + 1)
                quality = inspect_image(raw, upload.content_type)
                codes = warnings.get(slot, [])
                allowed = {
                    "SMALL",
                    "DARK",
                    "BRIGHT",
                    "LOW_DETAIL",
                    "FACE_MANUAL_CHECK",
                    "FACE_COUNT_UNCONFIRMED",
                }
                if not isinstance(codes, list) or any(
                    not isinstance(code, str) or code not in allowed for code in codes
                ):
                    raise HTTPException(422, "Cảnh báo ảnh không hợp lệ.")
                quality["client_warning_codes"] = sorted(set(codes))
                updates.append((slot, upload.content_type, raw, json.dumps(quality)))

        with database(path) as db:
            db.execute("BEGIN IMMEDIATE")
            current = find(db, key, actor)
            if current["last_key"] == retry_key:
                return public(current)
            if current["status"] not in EDITABLE or revision != current["revision"]:
                raise HTTPException(409, "Phiên bản hồ sơ đã thay đổi.")
            existing = {
                r[0] for r in db.execute("SELECT slot FROM captures WHERE application_id=?", (key,))
            }
            if not REQUIRED <= existing | {x[0] for x in updates}:
                raise HTTPException(422, "Thiếu ảnh bắt buộc.")
            if remove_optional:
                db.execute(
                    "DELETE FROM captures WHERE application_id=? AND slot='gplx_back'", (key,)
                )
            for update in updates:
                db.execute("INSERT OR REPLACE INTO captures VALUES (?,?,?,?,?)", (key, *update))
            db.execute(
                "UPDATE applications SET status='PENDING_REVIEW', revision=revision+1, "
                "submitted_at=?,reviewed_at=NULL,details=?,reason=NULL,last_key=? WHERE id=?",
                (now(), fields.model_dump_json(), retry_key, key),
            )
            observations = {}
            for run in db.execute(
                "SELECT slot,image_sha,result FROM extraction_runs "
                "WHERE application_id=? AND revision=?",
                (key, revision),
            ):
                capture = db.execute(
                    "SELECT data FROM captures WHERE application_id=? AND slot=?", (key, run[0])
                ).fetchone()
                if capture and hashlib.sha256(capture[0]).hexdigest() == run[1]:
                    observations[run[0]] = json.loads(run[2])
            confirmed = fields.model_dump(mode="json")
            prior = canonical_details(json.loads(current["details"] or "{}"))
            proposed = suggestions(observations.values())
            field_rows = {}
            review_values = {
                key: value
                for key, value in confirmed.items()
                if key not in {"no_expiry", "expiry_date"}
            }
            review_values["gplx_expiry"] = {
                "no_expiry": confirmed["no_expiry"],
                "expiry_date": confirmed["expiry_date"],
            }
            if prior:
                prior["gplx_expiry"] = {key: prior.get(key) for key in ("no_expiry", "expiry_date")}
            for field, value in review_values.items():
                proposal = proposed.get(field, {})
                ocr_warnings = ["OCR_REVIEW_REQUIRED"] if proposal.get("ocr_options") else []
                if field == "gplx_expiry":
                    observation = observations.get("gplx_front", {}).get("ocr", {})
                    issue = observation.get("field_statuses", {}).get(field)
                    if not issue and observation.get("status") in {"OCR_FAILED", "OCR_TIMEOUT"}:
                        issue = observation["status"]
                    if issue and issue not in {"READ", "NO_EXPIRY"}:
                        ocr_warnings.append(issue)
                source_matches = [
                    source
                    for source in ("qr", "ocr")
                    if value in proposal.get(source + "_options", [])
                ]
                options = proposal.get("qr_options", []) + proposal.get("ocr_options", [])
                field_rows[field] = {
                    "qr_value": None,
                    "ocr_value": None,
                    "suggested_value": None,
                    "extraction_status": "MANUAL",
                    **proposal,
                    "confirmed_value": value,
                    "warning_codes": proposal.get("warning_codes", []) + ocr_warnings,
                    "edited_by_user": bool(options and not source_matches)
                    or (field == "gplx_name" and name_copy is not None),
                    "confirmed_sources": source_matches or ["manual"],
                    "changed_since_previous_submission": field in prior and prior[field] != value,
                }
            original_warnings = comparison_warnings(
                {field: row["suggested_value"] for field, row in proposed.items()}
            )
            if name_copy is not None and not any(
                code in original_warnings
                for code in ("DIACRITIC_MISMATCH", "NAME_MISMATCH", "NAME_AND_DOB_MISMATCH")
            ):
                original_warnings.append("DIACRITIC_MISMATCH")
            confirmed_warnings = comparison_warnings(confirmed)
            db.execute(
                "INSERT INTO extraction_reviews VALUES (?,?,?)",
                (
                    key,
                    revision + 1,
                    json.dumps(
                        {
                            "observations": observations,
                            "fields": field_rows,
                            "cross_document_conflicts": confirmed_warnings,
                            "original_warning_codes": original_warnings,
                            "gplx_name_edit": (
                                {"action": "COPY_CCCD_NAME_CONFIRMED", "previous_value": name_copy}
                                if name_copy is not None
                                else None
                            ),
                        }
                    ),
                ),
            )
            db.execute("DELETE FROM extraction_runs WHERE application_id=?", (key,))
            audit(db, key, actor, "USER_CONFIRMATION_SYNTHETIC_DEMO", revision + 1)
            audit(db, key, actor, "CONFIRMED_FIELDS_AND_CAPTURES", revision + 1)
            if name_copy is not None:
                audit(db, key, actor, "COPY_CCCD_NAME_CONFIRMED", revision + 1)
            audit(db, key, actor, "PENDING_REVIEW", revision + 1)
            return public(find(db, key, actor))

    @app.post("/api/applications/{application_id}/decision")
    def decision(application_id: UUID, value: Decision, actor=actor_dependency):
        require(actor, "admin")
        action, reason = value.action, value.reason.strip()
        if action not in {"APPROVED", "REJECTED", "NEEDS_RESUBMISSION"} or (
            action != "APPROVED" and not reason
        ):
            raise HTTPException(422, "Chọn quyết định và nhập lý do khi cần.")
        key = str(application_id)
        with database(path) as db:
            db.execute("BEGIN IMMEDIATE")
            row = find(db, key, actor)
            if row["status"] != "PENDING_REVIEW" or row["revision"] != value.revision:
                raise HTTPException(409, "Hồ sơ đã đổi hoặc đã được duyệt.")
            db.execute(
                "UPDATE applications SET status=?,reviewed_at=?,reason=? WHERE id=?",
                (action, now(), reason, key),
            )
            audit(db, key, actor, action, value.revision, reason)
            return public(find(db, key, actor))

    app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
    return app


def main():
    import uvicorn

    setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
    tokens = {
        os.environ["DRIVER_DEMO_DRIVER_TOKEN"]: ("driver-demo", "driver"),
        os.environ["DRIVER_DEMO_ADMIN_TOKEN"]: ("admin-demo", "admin"),
    }
    app = create_app(os.environ.get("DRIVER_DEMO_DB", "local_samples/driver-demo.db"), tokens)
    port = int(os.environ.get("DRIVER_DEMO_PORT", "8082"))
    uvicorn.run(app, host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
