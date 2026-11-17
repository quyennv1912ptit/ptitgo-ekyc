from fastapi import APIRouter, Request, Response

router = APIRouter()


def _deployment_metadata(request: Request) -> dict[str, str]:
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return {}
    metadata: dict[str, str] = {}
    if getattr(settings, "app_version", None):
        metadata["version"] = settings.app_version
    if getattr(settings, "git_commit_sha", None):
        metadata["commitSha"] = settings.git_commit_sha
    return metadata


@router.get("/health")
def health(request: Request) -> dict[str, str]:
    return {"status": "ok", **_deployment_metadata(request)}


@router.get("/health/ready")
def readiness(request: Request, response: Response) -> dict[str, str]:
    metadata = _deployment_metadata(request)
    error = getattr(request.app.state, "inference_readiness_error", None)
    if error:
        response.status_code = 503
        return {"status": "unavailable", "worker": "blocked", "code": error, **metadata}
    worker = getattr(request.app.state, "cccd_worker", None)
    ready, state = worker.readiness() if worker is not None else (False, "disabled")
    if not ready:
        response.status_code = 503
    return {"status": "ready" if ready else "unavailable", "worker": state, **metadata}
