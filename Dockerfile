FROM python:3.11-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
ARG UV_SYNC_LOCK_MODE=--locked
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync "${UV_SYNC_LOCK_MODE}" --no-default-groups --group inference --no-install-project
COPY src ./src
RUN uv sync "${UV_SYNC_LOCK_MODE}" --no-default-groups --group inference --no-editable

FROM python:3.11-slim AS runtime
RUN groupadd -g 10001 ekyc && useradd -u 10001 -g ekyc -m ekyc
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
ENV ENV=staging PATH="/app/.venv/bin:$PATH" HOME=/home/ekyc PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 \
    YOLO_CONFIG_DIR=/tmp/ekyc-yolo MPLCONFIGDIR=/tmp/ekyc-mpl \
    EASYOCR_MODULE_PATH=/tmp/ekyc-easyocr \
    YOLO_AUTOINSTALL=false YOLO_OFFLINE=true
USER 10001:10001
EXPOSE 8001
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3).close()"]
CMD ["python", "-m", "uvicorn", "ekyc.main:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "1", "--timeout-graceful-shutdown", "35", "--no-access-log"]
