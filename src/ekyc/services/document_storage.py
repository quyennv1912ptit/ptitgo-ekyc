"""Private object store contract and local filesystem implementation."""

import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken


class DocumentStorage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def read(self, key: str) -> bytes: ...
    def delete(self, key: str) -> None: ...
    def objects(self) -> Iterator[tuple[str, float]]: ...


class LocalDocumentStorage:
    def __init__(self, root: Path, key: str | None = None):
        self.root = root.resolve()
        demo_frontend = (Path(__file__).resolve().parents[3] / "frontend").resolve()
        if self.root.is_relative_to(demo_frontend):
            raise ValueError("Document storage must be outside the web root")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cipher = Fernet(key.encode()) if key is not None else None
        self.key_fingerprint = hashlib.sha256(key.encode()).hexdigest() if key else "development"

    def bind(self, identifier: str) -> None:
        marker = self.root / ".verification-store"
        if marker.is_symlink():
            raise OSError("Unexpected storage link")
        binding = f"{identifier}:{self.key_fingerprint}"
        if not marker.exists() and any(self.root.iterdir()) and not marker.exists():
            raise ValueError("Use an empty dedicated verification storage directory")
        try:
            with marker.open("x", encoding="ascii") as file:
                file.write(binding)
                file.flush()
                os.fsync(file.fileno())
        except FileExistsError:
            if marker.read_text(encoding="ascii") != binding:
                raise ValueError("Storage binding or encryption key mismatch") from None

    def _path(self, key: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}|stage-[a-zA-Z0-9_-]{8,64}\.tmp", key):
            raise ValueError("Invalid internal storage key")
        path = self.root / key
        if path.is_symlink():
            raise OSError("Unexpected storage link")
        return path

    def put(self, key: str, data: bytes) -> None:
        target = self._path(key)
        if target.exists():
            raise FileExistsError("Storage key already exists")
        data = b"E1" + self.cipher.encrypt(data) if self.cipher is not None else b"D1" + data
        with tempfile.NamedTemporaryFile(
            prefix="stage-", suffix=".tmp", dir=self.root, delete=False
        ) as staged:
            partial = Path(staged.name)
            try:
                with staged:
                    staged.write(data)
                    staged.flush()
                    os.fsync(staged.fileno())
                os.replace(partial, target)
            finally:
                partial.unlink(missing_ok=True)

    def read(self, key: str) -> bytes:
        data = self._path(key).read_bytes()
        if self.cipher is None and data.startswith(b"D1"):
            return data[2:]
        if self.cipher is not None and data.startswith(b"E1"):
            try:
                return self.cipher.decrypt(data[2:])
            except InvalidToken:
                raise OSError("Object authentication failed") from None
        raise OSError("Object encoding does not match storage configuration")

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def objects(self) -> Iterator[tuple[str, float]]:
        for path in self.root.iterdir():
            try:
                if self._path(path.name).is_file():
                    yield path.name, path.stat().st_mtime
            except (ValueError, OSError):
                continue
