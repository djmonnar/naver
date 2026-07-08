import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


TRUE_VALUES = {"1", "true", "yes", "on"}


def load_dotenv() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def get_data_backend() -> str:
    return os.environ.get("DATA_BACKEND", "sqlite").strip().lower()


def uses_firestore() -> bool:
    return get_data_backend() in {"firebase", "firestore"}


def _load_service_account_json() -> dict[str, Any] | None:
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT_JSON")
    if not raw:
        return None
    return json.loads(raw)


@lru_cache(maxsize=1)
def get_admin_app():
    import firebase_admin
    from firebase_admin import credentials

    if firebase_admin._apps:
        return firebase_admin.get_app()

    service_account = _load_service_account_json()
    service_account_file = (
        os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    )

    if service_account:
        cred = credentials.Certificate(service_account)
        project_id = service_account.get("project_id")
    elif service_account_file:
        cred = credentials.Certificate(service_account_file)
        project_id = os.environ.get("FIREBASE_PROJECT_ID")
    else:
        cred = credentials.ApplicationDefault()
        project_id = os.environ.get("FIREBASE_PROJECT_ID")

    options = {}
    if project_id:
        options["projectId"] = project_id

    try:
        return firebase_admin.initialize_app(cred, options)
    except Exception as exc:
        raise RuntimeError(
            "Firebase Admin 초기화에 실패했습니다. "
            "FIREBASE_SERVICE_ACCOUNT_JSON 또는 FIREBASE_SERVICE_ACCOUNT_FILE 값을 확인하세요."
        ) from exc


def get_web_config() -> dict[str, str]:
    raw = os.environ.get("FIREBASE_WEB_CONFIG_JSON")
    if raw:
        loaded = json.loads(raw)
        return {k: str(v) for k, v in loaded.items() if v is not None}

    pairs = {
        "apiKey": "FIREBASE_API_KEY",
        "authDomain": "FIREBASE_AUTH_DOMAIN",
        "projectId": "FIREBASE_PROJECT_ID",
        "storageBucket": "FIREBASE_STORAGE_BUCKET",
        "messagingSenderId": "FIREBASE_MESSAGING_SENDER_ID",
        "appId": "FIREBASE_APP_ID",
    }
    return {
        firebase_key: os.environ[env_key]
        for firebase_key, env_key in pairs.items()
        if os.environ.get(env_key)
    }


def web_config_ready() -> bool:
    config = get_web_config()
    required = {"apiKey", "authDomain", "projectId", "appId"}
    return required.issubset(config.keys())
