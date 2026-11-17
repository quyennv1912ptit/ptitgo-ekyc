"""Private Modal entry point for eKYC document extraction."""

import os
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parents[1]
VOLUME_MOUNT = "/opt/ekyc"
CHECKPOINT = f"{VOLUME_MOUNT}/models/card_pose_v3_best.pt"
OCR_DIR = f"{VOLUME_MOUNT}/ocr"
LOCAL_CHECKPOINT = ROOT / "models/card_pose_v3_best.pt"
OCR_SOURCE = Path(os.environ.get("EKYC_OCR_DIR", Path.home() / ".EasyOCR" / "model"))
DEFAULT_APP_NAME = "ekyc-extraction-staging"
DEFAULT_SECRET_NAME = "ekyc-extraction-staging"
DEFAULT_VOLUME_NAME = "ekyc-models"


def local_models_available() -> bool:
    return (
        LOCAL_CHECKPOINT.is_file()
        and (OCR_SOURCE / "craft_mlt_25k.pth").is_file()
        and (OCR_SOURCE / "latin_g2.pth").is_file()
    )


def resolve_model_source() -> str:
    configured = os.environ.get("MODAL_MODEL_SOURCE", "").strip().lower()
    if configured in {"volume", "local"}:
        if configured == "local" and not local_models_available():
            raise FileNotFoundError("Local eKYC weights not found for MODAL_MODEL_SOURCE=local")
        return configured
    if configured:
        raise ValueError(f"Unsupported MODAL_MODEL_SOURCE: {configured}")
    if os.environ.get("CI", "").strip().lower() in {"1", "true", "yes"}:
        return "volume"
    return "local" if local_models_available() else "volume"


def resolve_app_env() -> str:
    env_name = os.environ.get("MODAL_APP_ENV", "staging").strip().lower() or "staging"
    if env_name not in {"staging", "prod"}:
        raise ValueError(f"Unsupported MODAL_APP_ENV: {env_name}")
    return env_name


def build_image_env() -> dict[str, str]:
    env = {
        "PYTHONPATH": "/app/.venv/lib/python3.11/site-packages",
        "ENV": resolve_app_env(),
        "OCR_BACKEND": "easyocr",
        "CARD_DETECTOR_BACKEND": "yolo",
        "CARD_YOLO_MODEL_PATH": CHECKPOINT,
        "EASYOCR_MODEL_DIR": OCR_DIR,
        "INFERENCE_WARM_UP": "true",
        "CCCD_TIMEOUT_S": "34",
    }
    deploy_tag = os.environ.get("MODAL_DEPLOY_TAG", "").strip()
    commit_sha = os.environ.get("GIT_COMMIT_SHA", "").strip()
    if deploy_tag:
        env["APP_VERSION"] = deploy_tag
    if commit_sha:
        env["GIT_COMMIT_SHA"] = commit_sha
    return env


def resolve_modal_environment() -> str | None:
    env_name = os.environ.get("MODAL_ENVIRONMENT", "").strip()
    return env_name or None


def build_secrets() -> list[modal.Secret]:
    modal_env = resolve_modal_environment()
    secret_name = (
        os.environ.get("MODAL_SECRET_NAME", DEFAULT_SECRET_NAME).strip() or DEFAULT_SECRET_NAME
    )
    secrets = [
        modal.Secret.from_name(
            secret_name,
            environment_name=modal_env,
            required_keys=["EKYC_API_KEY"],
        )
    ]
    extra = os.environ.get("MODAL_EXTRA_SECRETS", "").strip()
    for item in extra.split(","):
        name = item.strip()
        if name and name != secret_name:
            secrets.append(modal.Secret.from_name(name, environment_name=modal_env))
    return secrets


def build_volumes(model_source: str) -> dict[str, modal.Volume]:
    if model_source != "volume":
        return {}
    modal_env = resolve_modal_environment()
    volume_name = (
        os.environ.get("MODAL_MODEL_VOLUME_NAME", DEFAULT_VOLUME_NAME).strip()
        or DEFAULT_VOLUME_NAME
    )
    return {
        VOLUME_MOUNT: modal.Volume.from_name(
            volume_name,
            environment_name=modal_env,
            create_if_missing=False,
        ).read_only()
    }


MODEL_SOURCE = resolve_model_source()
APP_NAME = os.environ.get("MODAL_APP_NAME", DEFAULT_APP_NAME).strip() or DEFAULT_APP_NAME

image = modal.Image.from_dockerfile(
    ROOT / "Dockerfile", context_dir=ROOT, build_args={"UV_SYNC_LOCK_MODE": "--frozen"}
).env(build_image_env())
if MODEL_SOURCE == "local":
    image = (
        image.add_local_file(LOCAL_CHECKPOINT, CHECKPOINT, copy=True)
        .add_local_file(OCR_SOURCE / "craft_mlt_25k.pth", f"{OCR_DIR}/craft_mlt_25k.pth", copy=True)
        .add_local_file(OCR_SOURCE / "latin_g2.pth", f"{OCR_DIR}/latin_g2.pth", copy=True)
    )

app = modal.App(APP_NAME)


@app.function(
    image=image,
    secrets=build_secrets(),
    volumes=build_volumes(MODEL_SOURCE),
    cpu=4,
    memory=(3072, 6144),
    min_containers=0,
    max_containers=1,
    scaledown_window=120,
    region="ap",
    routing_region="ap-south",
    startup_timeout=120,
    timeout=120,
)
@modal.concurrent(max_inputs=2, target_inputs=1)
@modal.asgi_app(requires_proxy_auth=True)
def cccd_api():
    from ekyc.main import app as fastapi_app

    return fastapi_app
