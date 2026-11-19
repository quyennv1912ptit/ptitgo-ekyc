import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from ekyc.config import Settings

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"


def _load_script(name: str, env: dict[str, str] | None = None, monkeypatch=None):
    if monkeypatch is not None and env:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
    module_name = f"test_script_{name}_{id(env)}"
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_health_endpoints_include_deployment_metadata_only_when_configured(monkeypatch):
    monkeypatch.setenv("EKYC_API_KEY", "synthetic-test-key-for-staging-only")
    from ekyc.main import create_app

    base_settings = Settings(
        _env_file=None,
        env="staging",
        ekyc_api_key="synthetic-test-key-for-staging-only",
        ocr_backend="none",
    )
    with TestClient(create_app(base_settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/health/ready").json() == {"status": "unavailable", "worker": "disabled"}

    tagged_settings = base_settings.model_copy(
        update={
            "app_version": "sha-abcdef123456",
            "git_commit_sha": "abcdef1234567890abcdef1234567890abcdef12",
        }
    )
    with TestClient(create_app(tagged_settings)) as client:
        assert client.get("/health").json() == {
            "status": "ok",
            "version": "sha-abcdef123456",
            "commitSha": "abcdef1234567890abcdef1234567890abcdef12",
        }
        assert client.get("/health/ready").json() == {
            "status": "unavailable",
            "worker": "disabled",
            "version": "sha-abcdef123456",
            "commitSha": "abcdef1234567890abcdef1234567890abcdef12",
        }


def test_modal_pre_deploy_offline_checks(monkeypatch):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    pre_check = _load_script("modal_pre_deploy_check")

    manifests = pre_check.verify_manifest_constants()
    assert manifests["inference_artifacts"] == [
        "models/card_pose_v3_best.pt",
        "ocr/craft_mlt_25k.pth",
        "ocr/latin_g2.pth",
    ]
    assert manifests["face_artifacts"] == [
        "face_detection_yunet_2023mar.onnx",
        "face_recognition_sface_2021dec.onnx",
    ]


@dataclass
class _FakeFileEntry:
    path: str
    size: int


class _FakeVolume:
    def __init__(
        self,
        entries: dict[str, list[_FakeFileEntry]],
        files: dict[str, bytes] | None = None,
    ):
        self.entries = entries
        self.files = files or {}

    def listdir(self, prefix: str):
        if prefix not in self.entries:
            raise FileNotFoundError(prefix)
        return self.entries[prefix]

    def read_file(self, path: str):
        yield self.files[path]


def test_modal_pre_deploy_volume_verification():
    pre_check = _load_script("modal_pre_deploy_check")
    valid_volume = _FakeVolume(
        {
            "models": [_FakeFileEntry("models/card_pose_v3_best.pt", 6_417_624)],
            "ocr": [
                _FakeFileEntry("ocr/craft_mlt_25k.pth", 83_152_330),
                _FakeFileEntry("ocr/latin_g2.pth", 15_406_141),
            ],
        }
    )
    result = pre_check.verify_volume_contents(valid_volume)
    assert result["face_manifest_verified"] is False
    assert len(result["verified_files"]) == 3

    missing_volume = _FakeVolume({"models": [], "ocr": []})
    with pytest.raises(RuntimeError, match="missing required non-empty weights"):
        pre_check.verify_volume_contents(missing_volume)

    face_manifest = json.dumps(
        {
            "detector": "face_detection_yunet_2023mar.onnx",
            "detector_sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
            "recognizer": "face_recognition_sface_2021dec.onnx",
            "recognizer_sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
            "detector_max_side": 640,
            "detector_min_score": 0.9,
            "provenance_reference": "https://github.com/opencv/opencv_zoo",
            "quality": {
                "version": "v1",
                "min_face_pixels": 20,
                "min_blur_score": 30,
                "max_glare_fraction": 0.2,
                "min_brightness": 30,
            },
        }
    ).encode("utf-8")
    face_volume = _FakeVolume(
        {
            "models": [_FakeFileEntry("models/card_pose_v3_best.pt", 10)],
            "ocr": [
                _FakeFileEntry("ocr/craft_mlt_25k.pth", 10),
                _FakeFileEntry("ocr/latin_g2.pth", 10),
            ],
            "face": [
                _FakeFileEntry("face/manifest.json", len(face_manifest)),
                _FakeFileEntry("face/face_detection_yunet_2023mar.onnx", 10),
                _FakeFileEntry("face/face_recognition_sface_2021dec.onnx", 10),
            ],
        },
        files={"face/manifest.json": face_manifest},
    )
    assert pre_check.verify_volume_contents(face_volume)["face_manifest_verified"] is True

    tampered_manifest = face_manifest.replace(
        b"8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        hashlib.sha256(b"tampered").hexdigest().encode("ascii"),
    )
    face_volume.files["face/manifest.json"] = tampered_manifest
    with pytest.raises(ValueError, match="Face detector SHA-256 mismatch"):
        pre_check.verify_volume_contents(face_volume)


def test_modal_post_deploy_verify_origin_validation():
    post_verify = _load_script("modal_post_deploy_verify")
    assert (
        post_verify.validate_modal_origin("https://ws--ekyc-extraction-staging-cccd-api.ap-south.modal.run/")
        == "https://ws--ekyc-extraction-staging-cccd-api.ap-south.modal.run"
    )
    for bad in (
        "http://ws--app.modal.run",
        "https://example.com",
        "https://ws--app.modal.run:8443",
        "https://ws--app.modal.run/extra",
        "https://ws--app.modal.run?x=1",
    ):
        with pytest.raises(ValueError):
            post_verify.validate_modal_origin(bad)


def test_modal_post_deploy_verify_flow_and_failures():
    post_verify = _load_script("modal_post_deploy_verify")
    expected_sha = "a" * 40
    expected_version = f"sha-{expected_sha[:12]}"
    ready_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal ready_calls
        if request.headers.get("Modal-Key") != "pk" or request.headers.get("Modal-Secret") != "ps":
            return httpx.Response(401, json={"error": "proxy auth required"})
        if request.url.path == "/health":
            return httpx.Response(
                200,
                json={"status": "ok", "version": expected_version, "commitSha": expected_sha},
            )
        if request.url.path == "/health/ready":
            ready_calls += 1
            if ready_calls == 1:
                return httpx.Response(503, json={"status": "unavailable", "worker": "starting"})
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "worker": "warm",
                    "version": expected_version,
                    "commitSha": expected_sha,
                },
            )
        if request.url.path == "/ekyc/metrics":
            if request.headers.get("X-API-Key") != "valid-key":
                return httpx.Response(401, json={"success": False, "errorCode": "UNAUTHORIZED"})
            return httpx.Response(200, text="ekyc_inference_queue_depth 0\n")
        if request.url.path == "/ekyc/documents/cccd_front/extract":
            if request.headers.get("X-API-Key") != "valid-key":
                return httpx.Response(401, json={"success": False, "errorCode": "UNAUTHORIZED"})
            return httpx.Response(
                200,
                json={
                    "success": False,
                    "errorCode": "CARD_NOT_FOUND",
                    "retryable": False,
                    "warnings": [],
                    "requestId": "req-1",
                },
            )
        return httpx.Response(404)

    with httpx.Client(
        base_url="https://ws--app.modal.run",
        transport=httpx.MockTransport(handler),
    ) as client:
        report = post_verify.verify_deployment(
            client,
            proxy_key="pk",
            proxy_secret="ps",
            api_key="valid-key",
            expected_sha=expected_sha,
            expected_version=expected_version,
            ready_timeout_s=5.0,
            poll_interval_s=0.0,
        )
    assert report["health"] == "ok"
    assert report["ready"] == "ready"
    assert report["worker"] == "warm"
    assert report["commitSha"] == expected_sha
    assert report["smokeHttp"] == 200
    assert report["smokeErrorCode"] == "CARD_NOT_FOUND"

    def bad_model_handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Modal-Key") != "pk":
            return httpx.Response(401)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/health/ready":
            return httpx.Response(
                503,
                json={"status": "unavailable", "worker": "blocked", "code": "MODEL_HASH_MISMATCH"},
            )
        return httpx.Response(404)

    with httpx.Client(
        base_url="https://ws--app.modal.run", transport=httpx.MockTransport(bad_model_handler)
    ) as client:
        with pytest.raises(RuntimeError, match="MODEL_HASH_MISMATCH"):
            post_verify.verify_deployment(
                client,
                proxy_key="pk",
                proxy_secret="ps",
                api_key="valid-key",
                ready_timeout_s=1.0,
                poll_interval_s=0.0,
            )


def test_deploy_modal_workflow_yaml_contract():
    workflow_path = ROOT / ".github" / "workflows" / "deploy-modal.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    # PyYAML parses unquoted top-level `on:` as boolean True under YAML 1.1 rules.
    triggers = workflow["on"] if "on" in workflow else workflow[True]
    assert list(triggers.keys()) == ["workflow_dispatch"]
    env_input = triggers["workflow_dispatch"]["inputs"]["environment"]
    assert env_input["options"] == ["staging"]
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["permissions"] == {}

    deploy_job = workflow["jobs"]["deploy"]
    assert deploy_job["environment"] == "${{ inputs.environment }}"
    assert deploy_job["if"] == "inputs.environment == 'staging'"

    for job in workflow["jobs"].values():
        for step in job["steps"]:
            uses = step.get("uses")
            if uses:
                _, ref = uses.split("@", 1)
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref)

    modal_secret_steps = [
        step["name"]
        for step in deploy_job["steps"]
        if "MODAL_TOKEN_ID" in (step.get("env") or {})
        or "MODAL_TOKEN_SECRET" in (step.get("env") or {})
    ]
    assert modal_secret_steps == ["Deploy to Modal"]
    assert "MODAL_TOKEN_ID" not in json.dumps(workflow["jobs"]["quality-gates"])


def test_modal_entrypoint_config_and_volume_mode(monkeypatch):
    class _FakeSecret:
        def __init__(self, name, environment_name=None, required_keys=None):
            self.name = name
            self.environment_name = environment_name
            self.required_keys = required_keys or []

    class _FakeVolumeRef:
        def __init__(self, name, environment_name=None, create_if_missing=True):
            self.name = name
            self.environment_name = environment_name
            self.create_if_missing = create_if_missing
            self.is_read_only = False

        def read_only(self):
            self.is_read_only = True
            return self

    class _FakeImage:
        def env(self, mapping):
            self.env_map = mapping
            return self

        def add_local_file(self, *args, **kwargs):
            return self

    class _FakeApp:
        def __init__(self, name):
            self.name = name
            self.fn_kwargs = None

        def function(self, **kwargs):
            self.fn_kwargs = kwargs
            return lambda fn: fn

    class _FakeModal:
        App = _FakeApp

        class Image:
            @staticmethod
            def from_dockerfile(*args, **kwargs):
                return _FakeImage()

        class Secret:
            @staticmethod
            def from_name(name, environment_name=None, required_keys=None):
                return _FakeSecret(
                    name, environment_name=environment_name, required_keys=required_keys
                )

        class Volume:
            @staticmethod
            def from_name(name, environment_name=None, create_if_missing=False):
                return _FakeVolumeRef(
                    name, environment_name=environment_name, create_if_missing=create_if_missing
                )

        @staticmethod
        def concurrent(**kwargs):
            return lambda fn: fn

        @staticmethod
        def asgi_app(**kwargs):
            return lambda fn: fn

    monkeypatch.setitem(sys.modules, "modal", _FakeModal)
    monkeypatch.setenv("MODAL_MODEL_SOURCE", "volume")
    monkeypatch.setenv("MODAL_ENVIRONMENT", "production")
    monkeypatch.setenv("MODAL_APP_ENV", "prod")
    monkeypatch.setenv("MODAL_MODEL_VOLUME_NAME", "ekyc-models-prod")
    monkeypatch.setenv("MODAL_SECRET_NAME", "ekyc-prod-secret")
    monkeypatch.setenv("MODAL_EXTRA_SECRETS", "ekyc-sync-secret")
    monkeypatch.setenv("MODAL_DEPLOY_TAG", "sha-0123456789ab")
    monkeypatch.setenv("GIT_COMMIT_SHA", "0123456789abcdef0123456789abcdef01234567")

    modal_ekyc = _load_script("modal_ekyc")
    assert modal_ekyc.app.name == "ekyc-extraction-staging"
    assert modal_ekyc.MODEL_SOURCE == "volume"
    fn_kwargs = modal_ekyc.app.fn_kwargs
    assert fn_kwargs["cpu"] == 4
    assert fn_kwargs["memory"] == (3072, 6144)
    assert fn_kwargs["min_containers"] == 0
    assert fn_kwargs["max_containers"] == 1
    assert fn_kwargs["scaledown_window"] == 300
    assert fn_kwargs["region"] == "ap"
    assert fn_kwargs["routing_region"] == "ap-south"

    mounted = fn_kwargs["volumes"]["/opt/ekyc"]
    assert mounted.name == "ekyc-models-prod"
    assert mounted.environment_name == "production"
    assert mounted.create_if_missing is False
    assert mounted.is_read_only is True

    secrets = fn_kwargs["secrets"]
    assert [s.name for s in secrets] == ["ekyc-prod-secret", "ekyc-sync-secret"]
    assert all(s.environment_name == "production" for s in secrets)
    assert secrets[0].required_keys == ["EKYC_API_KEY"]

    env_map = fn_kwargs["image"].env_map
    assert env_map["ENV"] == "prod"
    assert env_map["APP_VERSION"] == "sha-0123456789ab"
    assert env_map["GIT_COMMIT_SHA"] == "0123456789abcdef0123456789abcdef01234567"
    assert env_map["CARD_YOLO_MODEL_PATH"] == "/opt/ekyc/models/card_pose_v3_best.pt"
    assert env_map["EASYOCR_MODEL_DIR"] == "/opt/ekyc/ocr"

    monkeypatch.setenv("MODAL_APP_ENV", "dev")
    with pytest.raises(ValueError, match="Unsupported MODAL_APP_ENV"):
        modal_ekyc.resolve_app_env()

    monkeypatch.setenv("MODAL_SCALEDOWN_WINDOW", "420")
    assert modal_ekyc.resolve_scaledown_window() == 420
    for invalid_window in ("15", "901", "abc"):
        monkeypatch.setenv("MODAL_SCALEDOWN_WINDOW", invalid_window)
        with pytest.raises(ValueError, match="Unsupported MODAL_SCALEDOWN_WINDOW"):
            modal_ekyc.resolve_scaledown_window()
