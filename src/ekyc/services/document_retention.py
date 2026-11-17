"""Explicit operator-run retention and orphan reconciliation."""

from collections import Counter
from datetime import UTC, datetime, timedelta

from ekyc.config import Settings
from ekyc.services.verification_store import VerificationStore


def sweep(
    store: VerificationStore,
    settings: Settings,
    *,
    apply: bool = False,
    clock: datetime | None = None,
) -> dict[str, int]:
    clock = clock or datetime.now(UTC)
    stamp = clock.isoformat()
    retired = (clock - timedelta(days=settings.verification_retired_days)).isoformat()
    terminal = (
        (clock - timedelta(days=settings.verification_terminal_days)).isoformat()
        if settings.verification_terminal_days is not None
        else None
    )
    orphan = (clock - timedelta(hours=settings.verification_orphan_hours)).timestamp()
    with store.connect(write=True) as db:
        known = {
            row["object_key"]: row
            for row in db.execute("""
                SELECT o.object_key,o.retired_at,o.first_submitted_revision,a.status,a.updated_at
                FROM document_objects o JOIN applications a ON a.id=o.application_id
            """)
        }
        scheduled = {
            record["object_key"]: record["reason"]
            for record in db.execute("SELECT object_key,reason FROM document_deletions")
        }
        plan = {}
        for key, row in known.items():
            if key in scheduled:
                continue
            if (
                row["first_submitted_revision"] is None
                and row["retired_at"]
                and row["retired_at"] <= retired
            ):
                plan[key] = "retired_draft"
            elif (
                terminal
                and row["status"] in {"APPROVED", "REJECTED"}
                and row["updated_at"] <= terminal
            ):
                plan[key] = "terminal_retention"
        for key, modified in store.storage.objects():
            if key not in known and key not in scheduled and modified <= orphan:
                plan[key] = "orphan"
        counts = Counter(plan.values())
        if apply:
            db.executemany(
                "INSERT INTO document_deletions VALUES (?,?,?,NULL)",
                [(key, reason, stamp) for key, reason in plan.items()],
            )
        pending = [
            row["object_key"]
            for row in db.execute(
                "SELECT object_key FROM document_deletions WHERE completed_at IS NULL"
            )
        ]
    counts["pending"] = len(pending)
    if apply:
        for key in pending:
            with store.connect(write=True) as db:
                record = db.execute(
                    "SELECT completed_at FROM document_deletions WHERE object_key=?", (key,)
                ).fetchone()
                if record["completed_at"] is not None:
                    continue
                try:
                    store.storage.delete(key)
                except OSError:
                    counts["failed"] += 1
                    continue
                db.execute(
                    "UPDATE document_deletions SET completed_at=? WHERE object_key=?", (stamp, key)
                )
                counts["deleted"] += 1
    return dict(counts)
