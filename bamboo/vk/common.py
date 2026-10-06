"""Локальные файлы, строгий JSON, блокировки и безопасные ошибки."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


class VKError(Exception):
    """Только подготовленные сообщения; не включать ответы сервера и секреты."""


class Uncertain(VKError):
    """Сервер мог выполнить операцию. Повтор запрещён до сверки."""


class Rejected(VKError):
    """VK явно отклонил запрос."""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise VKError("Повторяющийся ключ JSON")
        result[key] = value
    return result


def loads(text):
    try:
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError) as exc:
        raise VKError("Некорректный JSON") from exc


def safe(root, relative):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or "\\" in str(relative):
        raise VKError("Нужен относительный путь внутри проекта")
    path = root
    for part in rel.parts:
        path /= part
        if path.is_symlink():
            raise VKError("Символические ссылки не разрешены")
    if not path.resolve().is_relative_to(root):
        raise VKError("Путь выходит за пределы проекта")
    return path


def read(path):
    try:
        if Path(path).is_symlink():
            raise VKError("Символические ссылки не разрешены")
        return loads(Path(path).read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise VKError("Локальный файл отсутствует или недоступен") from exc


def write(path, value):
    path = Path(path)
    if path.is_symlink():
        raise VKError("Символические ссылки не разрешены")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".vk-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(dumps(value) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise VKError("Операция уже запущена. После сбоя проверьте PID в .lock; не удаляйте блокировку вслепую") from exc
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(dumps({"pid": os.getpid(), "at": time.time()}))
        yield
    finally:
        path.unlink(missing_ok=True)


def local(root, name):
    return safe(root, ".bamboo/vk/" + name)


def config(root):
    value = read(local(root, "config.json"))
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1):
        raise VKError("Сначала выполните vk init")
    allowed = {"schema_version", "community", "group_id", "client_id", "redirect_uri", "scopes", "api_version"}
    if set(value) - allowed:
        raise VKError("Неизвестные поля конфигурации VK; секреты здесь хранить нельзя")
    if value.get("client_id") is not None and not str(value["client_id"]).isdigit():
        raise VKError("client_id должен быть числовым ID приложения")
    from urllib.parse import urlsplit
    redirect = urlsplit(value.get("redirect_uri", ""))
    if (redirect.scheme != "http" or redirect.hostname != "localhost" or redirect.username
            or redirect.password or redirect.query or redirect.fragment):
        raise VKError("Ожидается зарегистрированный http://localhost[/путь]")
    if (not isinstance(value.get("scopes"), str)
            or set(value["scopes"].split()) - {"wall", "photos", "groups", "market", "stats", "video", "docs"}):
        raise VKError("Некорректный перечень разрешений VK")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", str(value.get("community", ""))):
        raise VKError("Некорректное имя сообщества")
    if value.get("group_id") is not None and (type(value["group_id"]) is not int or value["group_id"] <= 0):
        raise VKError("group_id должен быть положительным целым числом")
    if value.get("api_version") != "5.199":
        raise VKError("Эта реализация проверяет контракт API 5.199; другую версию нельзя подставлять без проверки")
    return value
