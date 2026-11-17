import hashlib
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from ekyc.core.errors import EkycError, ErrorCode
from ekyc.services.document_retention import sweep
from ekyc.services.document_storage import LocalDocumentStorage
from ekyc.services.driver_auth import Actor
from ekyc.services.verification_store import VerificationStore

DRIVER = Actor("synthetic-driver", False)


def test_encrypted_object_dedup_and_failed_publication(settings, tmp_path, monkeypatch):
    storage = LocalDocumentStorage(tmp_path / "objects", Fernet.generate_key().decode())
    store = VerificationStore(tmp_path / "driver.sqlite3", storage)
    app = store.create(DRIVER)["id"]
    sample = b"synthetic-evidence"
    result = store.replace(app, DRIVER, 0, "cccd_front", "image/png", sample)
    assert result["revision"] == 1
    assert store.replace(app, DRIVER, 1, "cccd_front", "image/png", sample) == result
    keys = [key for key, _ in storage.objects()]
    assert len(keys) == 1
    assert sample not in (storage.root / keys[0]).read_bytes()
    assert store.capture(app, DRIVER, "cccd_front")[1] == sample

    with store.connect() as db:
        row = db.execute("SELECT * FROM document_objects").fetchone()
        assert row["owner"] == DRIVER.uid and row["created_revision"] == 1
        assert row["checksum"] == hashlib.sha256(sample).hexdigest()
    put = storage.put

    def failed_after_publish(key, data):
        put(key, data)
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(storage, "put", failed_after_publish)
    with pytest.raises(EkycError) as failure:
        store.replace(app, DRIVER, 1, "cccd_front", "image/png", b"synthetic other")
    assert failure.value.code == ErrorCode.VERIFICATION_UNAVAILABLE
    assert [key for key, _ in storage.objects()] == keys
    assert store.detail(app, DRIVER)["revision"] == 1


def test_retention_preserves_active_and_submitted_until_terminal_policy(settings, tmp_path):
    storage = LocalDocumentStorage(tmp_path / "objects")
    store = VerificationStore(tmp_path / "driver.sqlite3", storage)
    app = store.create(DRIVER)["id"]
    store.replace(app, DRIVER, 0, "cccd_front", "image/png", b"retired")
    store.replace(app, DRIVER, 1, "cccd_front", "image/png", b"active")
    store.replace(app, DRIVER, 2, "gplx_front", "image/png", b"submitted")
    store.replace(app, DRIVER, 3, "gplx_front", "image/png", b"replacement")
    orphan = uuid4().hex
    storage.put(orphan, b"orphan")
    clock = datetime.now(UTC) + timedelta(days=10)
    temp = storage.root / "stage-12345678.tmp"
    temp.write_bytes(b"partial")
    os.utime(temp, (clock.timestamp() - 60, clock.timestamp() - 60))
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE document_objects SET first_submitted_revision=3 "
            "WHERE slot='gplx_front' AND retired_at IS NOT NULL"
        )

    assert sweep(store, settings, clock=clock) == {"retired_draft": 1, "orphan": 1, "pending": 0}
    assert len(list(storage.objects())) == 6
    assert sweep(store, settings, apply=True, clock=clock)["deleted"] == 2
    assert store.capture(app, DRIVER, "cccd_front")[1] == b"active"
    assert temp.exists()
    assert len(list(storage.objects())) == 4

    with store.connect(write=True) as db:
        db.execute("UPDATE applications SET status='APPROVED' WHERE id=?", (app,))
    policy = settings.model_copy(update={"verification_terminal_days": 7})
    result = sweep(store, policy, apply=True, clock=clock)
    assert result["terminal_retention"] == result["deleted"] == 3
    with pytest.raises(EkycError) as expired:
        store.capture(app, DRIVER, "gplx_front")
    assert expired.value.code == ErrorCode.DOCUMENT_REMOVED
    with store.connect() as db:
        audit = db.execute(
            "SELECT count(*) FROM document_deletions WHERE completed_at IS NOT NULL"
        ).fetchone()[0]
        assert audit == 5


def test_storage_binding_refuses_different_database_or_key(settings, tmp_path):
    key = SecretStr(Fernet.generate_key().decode())
    config = settings.model_copy(
        update={
            "verification_db_path": tmp_path / "one.sqlite3",
            "verification_storage_dir": tmp_path / "objects",
            "verification_storage_key": key,
        }
    )
    VerificationStore.from_settings(config)
    VerificationStore.from_settings(config)
    with pytest.raises(ValueError, match="binding"):
        VerificationStore.from_settings(
            config.model_copy(update={"verification_db_path": tmp_path / "other.sqlite3"})
        )
    with pytest.raises(ValueError, match="binding"):
        VerificationStore.from_settings(
            config.model_copy(
                update={"verification_storage_key": SecretStr(Fernet.generate_key().decode())}
            )
        )


def test_legacy_blob_is_readable_and_excluded_from_retention(settings, tmp_path):
    storage = LocalDocumentStorage(tmp_path / "objects")
    store = VerificationStore(tmp_path / "driver.sqlite3", storage)
    app = store.create(DRIVER)["id"]
    payload = (
        '{"captures":{"selfie":{"id":"legacy","mime":"image/png"}},"proposals":{},"confirmed":null}'
    )
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO evidence VALUES (?,?,?,?,0)", ("legacy", app, "image/png", b"legacy")
        )
        db.execute("UPDATE applications SET payload=? WHERE id=?", (payload, app))
    assert store.capture(app, DRIVER, "selfie")[1] == b"legacy"
    store.replace(app, DRIVER, 0, "selfie", "image/png", b"new")
    clock = datetime.now(UTC) + timedelta(days=200)
    sweep(store, settings, apply=True, clock=clock)
    with store.connect() as db:
        assert db.execute("SELECT data FROM evidence WHERE id='legacy'").fetchone()[0] == b"legacy"
