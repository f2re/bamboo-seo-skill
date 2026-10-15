"""Ключи Instagram и Yandex Object Storage — в защищённом файле вне репозитория."""
from __future__ import annotations

import os
import time
from pathlib import Path

from ..vk.auth import load_private
from ..vk.common import locked, write
from .errors import IGError
from .graph import REFRESH_AFTER, REFRESH_WHEN_LEFT, refresh

LONG_LIVED = 60 * 24 * 3600


def secrets_path(root) -> Path:
    path = Path(os.environ.get("BAMBOO_IG_SECRETS_FILE", str(Path.home() / ".config/bamboo/ig.json"))).expanduser().absolute()
    if path.resolve().is_relative_to(Path(root).resolve()):
        raise IGError("Файл ключей должен находиться вне репозитория")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise IGError("Символические ссылки в пути к ключам запрещены")
    return path


def load(root) -> dict:
    path = secrets_path(root)
    return load_private(path) if path.exists() else {}


def _save(root, update: dict) -> None:
    path = secrets_path(root)
    with locked(path.with_suffix(".lock")):
        data = load_private(path) if path.exists() else {}
        write(path, {**data, **update})


def clean_value(value: str, what: str) -> str:
    value = (value or "").strip()
    if not value or any(c.isspace() for c in value) or "://" in value:
        raise IGError(f"Вставьте только {what}, без адреса и пробелов")
    return value


def import_token(root, value: str) -> dict:
    token = clean_value(value, "ключ доступа Instagram")
    if "=" in token or "&" in token:
        raise IGError("Вставьте только значение ключа, без access_token= и параметров")
    _save(root, {"access_token": token, "obtained_at": time.time(), "expires_at": time.time() + LONG_LIVED})
    return {"saved": True, "next": "ig check"}


def import_storage(root, key_id: str, secret: str) -> dict:
    _save(root, {"storage_key_id": clean_value(key_id, "идентификатор ключа"),
                 "storage_secret": clean_value(secret, "секретный ключ")})
    return {"saved": True, "next": "ig check"}


def token(root, opener=None) -> str:
    supplied = os.environ.get("BAMBOO_IG_ACCESS_TOKEN")
    if supplied:
        return supplied
    data = load(root)
    if not data.get("access_token"):
        raise IGError("Ключ Instagram отсутствует: выполните ig token-import")
    now = time.time()
    if data.get("expires_at") and data["expires_at"] <= now:
        raise IGError("Ключ Instagram истёк: получите новый и выполните ig token-import")
    if (now - data.get("obtained_at", now) >= REFRESH_AFTER
            and data.get("expires_at", now) - now <= REFRESH_WHEN_LEFT):
        result = refresh(data["access_token"], opener=opener)
        _save(root, {"access_token": result["access_token"], "obtained_at": now, "expires_at": now + result["expires_in"]})
        return result["access_token"]
    return data["access_token"]


def storage_keys(root) -> tuple[str, str]:
    key_id, secret = os.environ.get("BAMBOO_YC_KEY_ID"), os.environ.get("BAMBOO_YC_SECRET")
    if key_id and secret:
        return key_id, secret
    data = load(root)
    if not data.get("storage_key_id") or not data.get("storage_secret"):
        raise IGError("Ключи Yandex Object Storage отсутствуют: выполните ig storage-key-import")
    return data["storage_key_id"], data["storage_secret"]
