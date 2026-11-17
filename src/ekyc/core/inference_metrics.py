"""Request-scoped runtime measurements. Only static stage names are recorded."""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

timings: ContextVar[dict[str, float] | None] = ContextVar("inference_timings", default=None)


@contextmanager
def stage(name: str) -> Iterator[None]:
    values = timings.get()
    if values is None:
        yield
        return
    started = monotonic()
    try:
        yield
    finally:
        values[name] = values.get(name, 0.0) + (monotonic() - started) * 1000


def peak_ram_mib() -> float | None:
    if sys.platform == "win32":
        # psutil is supplied by the locked inference stack.
        import psutil

        try:
            peak = getattr(psutil.Process().memory_info(), "peak_wset", None)
        except psutil.Error:
            return None
        return peak / 2**20 if peak is not None else None

    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (2**20 if sys.platform == "darwin" else 1024)
