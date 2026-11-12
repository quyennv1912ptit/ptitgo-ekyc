from collections.abc import Iterator
from contextlib import contextmanager
from threading import BoundedSemaphore

from ekyc.core.errors import EkycError, ErrorCode


@contextmanager
def job_slot(sem: BoundedSemaphore, timeout: float) -> Iterator[None]:
    if not sem.acquire(timeout=timeout):
        raise EkycError(ErrorCode.ENGINE_BUSY)
    try:
        yield
    finally:
        sem.release()
