"""VK ID PKCE и локальные ключи. Пароли аккаунта никогда не принимаются."""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import stat
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from .common import VKError, config, locked, read, write
from .transport import form, request_json


def credential_path(root):
    path = Path(os.environ.get("BAMBOO_VK_TOKEN_FILE", str(Path.home() / ".config/bamboo/vk-token.json"))).expanduser().absolute()
    if path.resolve().is_relative_to(Path(root).resolve()):
        raise VKError("Файл ключа должен находиться вне репозитория; используйте домашний каталог")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise VKError("Символические ссылки в пути к ключу запрещены")
    return path


def load_private(path):
    if os.name == "posix" and path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise VKError("Файл ключа доступен другим пользователям. Установите chmod 600")
    value = read(path)
    if not isinstance(value, dict):
        raise VKError("Некорректный файл ключа")
    return value


def save_tokens(path, result, device_id, client_id, redirect_uri, old=None):
    if not isinstance(result.get("access_token"), str) or not result["access_token"]:
        raise VKError("VK не вернул access_token")
    expires = result.get("expires_in")
    if type(expires) not in (int, float) or expires < 0:
        raise VKError("VK не вернул корректное время действия ключа")
    data = {"access_token": result["access_token"], "refresh_token": result.get("refresh_token") or (old or {}).get("refresh_token"),
            "expires_at": time.time() + expires if expires else None,
            "device_id": device_id, "client_id": str(client_id), "redirect_uri": redirect_uri,
            "scope": result.get("scope", (old or {}).get("scope")), "user_id": result.get("user_id")}
    write(path, data)


def token(root):
    supplied = os.environ.get("BAMBOO_VK_ACCESS_TOKEN")
    if supplied:
        return supplied
    path = credential_path(root)
    with locked(path.with_suffix(".lock")):
        data = load_private(path)
        if not isinstance(data.get("access_token"), str) or not data["access_token"]:
            raise VKError("Ключ отсутствует; выполните vk login или vk token-import")
        if data.get("expires_at") and data["expires_at"] <= time.time() + 90:
            if not data.get("refresh_token"):
                raise VKError("Ключ истёк; повторите авторизацию")
            state = secrets.token_urlsafe(32)
            query = {"grant_type": "refresh_token", "client_id": data["client_id"],
                     "redirect_uri": data["redirect_uri"], "device_id": data["device_id"], "state": state}
            result = request_json("https://id.vk.com/oauth2/auth?" + urlencode(query), form({"refresh_token": data["refresh_token"]}))
            if "error" in result or not secrets.compare_digest(str(result.get("state", "")), state):
                raise VKError("Обновление ключа не подтверждено VK; повторите авторизацию")
            save_tokens(path, result, data["device_id"], data["client_id"], data["redirect_uri"], data)
            data = load_private(path)
        return data["access_token"]


def start(root, open_browser=False):
    cfg = config(root)
    if not str(cfg.get("client_id", "")).isdigit():
        raise VKError("Для VK ID нужен client_id из кабинета разработчика")
    redirect = cfg["redirect_uri"]
    parsed = urlsplit(redirect)
    if parsed.scheme != "http" or parsed.hostname != "localhost" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise VKError("Локальный redirect_uri: http://localhost[/путь], зарегистрированный в VK ID")
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    pending = {"state": state, "verifier": verifier, "created_at": time.time(),
               "client_id": str(cfg["client_id"]), "redirect_uri": redirect}
    path = credential_path(root).with_suffix(".pending.json")
    with locked(path.with_suffix(".lock")):
        write(path, pending)
    url = "https://id.vk.com/authorize?" + urlencode({"response_type": "code", "client_id": cfg["client_id"],
           "redirect_uri": redirect, "state": state, "code_challenge": challenge,
           "code_challenge_method": "S256", "scope": cfg.get("scopes", "wall photos groups market")})
    if open_browser:
        webbrowser.open(url)
    return {"authorize_url": url, "next": "После разрешения скопируйте полный адрес localhost из браузера. Выполните vk auth-finish и вставьте его локально, не в чат.",
            "note": "Сервер и sudo не нужны: ошибка подключения localhost ожидаема. Доступность redirect и scope определяет VK для вашего приложения."}


def finish(root, callback):
    path = credential_path(root)
    pending_path = path.with_suffix(".pending.json")
    with locked(pending_path.with_suffix(".lock")):
        pending = load_private(pending_path)
        if time.time() - pending["created_at"] > 600:
            raise VKError("Авторизация просрочена; выполните vk auth-start заново")
        parsed, expected = urlsplit(callback.strip()), urlsplit(pending["redirect_uri"])
        if (parsed.scheme, parsed.netloc, parsed.path or "/") != (expected.scheme, expected.netloc, expected.path or "/") or parsed.fragment:
            raise VKError("Адрес возврата не совпадает с зарегистрированным")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(len(v) != 1 for v in query.values()) or any(not query.get(k, [""])[0] for k in ("code", "device_id", "state")):
            raise VKError("Нужны единственные code, device_id и state в адресе возврата")
        if not secrets.compare_digest(query["state"][0], pending["state"]):
            raise VKError("state не совпадает; ответ авторизации отклонён")
        body = {"grant_type": "authorization_code", "client_id": pending["client_id"],
                "redirect_uri": pending["redirect_uri"], "state": pending["state"],
                "code_verifier": pending["verifier"], "device_id": query["device_id"][0]}
        # Одноразовый code не отправляется повторно даже при сбое обмена.
        pending_path.unlink()
        result = request_json("https://id.vk.com/oauth2/auth?" + urlencode(body), form({"code": query["code"][0]}))
        if "error" in result or not secrets.compare_digest(str(result.get("state", "")), pending["state"]):
            raise VKError("VK ID не подтвердил обмен кода; начните авторизацию заново")
        with locked(path.with_suffix(".lock")):
            save_tokens(path, result, query["device_id"][0], pending["client_id"], pending["redirect_uri"])
    return {"saved": True, "next": "vk check; успешный вход сам по себе не доказывает права wall/market/photos"}


def import_token(root, value):
    if not value or any(c.isspace() for c in value) or "access_token=" in value:
        raise VKError("Вставьте только значение пользовательского ключа, не URL и не пароль")
    path = credential_path(root)
    with locked(path.with_suffix(".lock")):
        write(path, {"access_token": value, "expires_at": None, "source": "manual"})
    return {"saved": True, "next": "vk check; импортированный ключ не имеет автоматического обновления"}
