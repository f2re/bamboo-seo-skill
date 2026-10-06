"""HTTPS без перенаправления секретов и без автоматического повтора записей."""
from __future__ import annotations

import mimetypes
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .common import VKError, Rejected, Uncertain, loads


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, data, content_type="application/x-www-form-urlencoded", mutation=False):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise VKError("Разрешён только HTTPS без логина и нестандартного порта")
    req = Request(url, data=data, headers={"Content-Type": content_type, "User-Agent": "Bamboo-VK/1.0"}, method="POST")
    try:
        with build_opener(NoRedirect()).open(req, timeout=30) as response:
            raw = response.read(10 * 1024 * 1024 + 1)
        if len(raw) > 10 * 1024 * 1024:
            raise ValueError()
        result = loads(raw.decode("utf-8"))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (HTTPError, URLError, OSError, ValueError, VKError) as exc:
        error = Uncertain if mutation else VKError
        raise error("Ответ HTTPS не получен или некорректен. Запись не повторяется автоматически") from exc


def form(params):
    data = {}
    for key, value in params.items():
        if isinstance(value, bool):
            value = int(value)
        elif isinstance(value, list):
            value = ",".join(str(x) for x in value)
        data[key] = str(value)
    return urlencode(data).encode()


class API:
    def __init__(self, token_provider, version="5.199", transport=request_json):
        self.token_provider = token_provider
        self.version = version
        self.transport = transport
        self.last_call = 0.0

    def call(self, method, params=None, mutation=False):
        from .registry import REGISTRY
        allowed = {x["method"] for x in REGISTRY.values()} | {
            "users.get", "groups.getById", "account.getAppPermissions",
            "photos.getMarketUploadServer", "photos.getWallUploadServer"}
        if method not in allowed:
            raise VKError("Метод отсутствует в разрешённом реестре")
        for attempt in range(3):
            delay = 0.4 - (time.monotonic() - self.last_call)
            if delay > 0:
                time.sleep(delay)
            token = self.token_provider()
            body = form({**(params or {}), "access_token": token, "v": self.version})
            self.last_call = time.monotonic()
            obj = self.transport("https://api.vk.com/method/" + method, body, mutation=mutation)
            if "error" in obj:
                code = obj["error"].get("error_code") if isinstance(obj["error"], dict) else None
                if code == 6 and not mutation and attempt < 2:
                    time.sleep(attempt + 1)
                    continue
                messages = {5: "Ключ недействителен: выполните vk login или vk token-import",
                            7: "Приложению не предоставлены необходимые права",
                            14: "VK требует CAPTCHA; решите её вручную, обход не выполняется",
                            15: "Нет доступа администратора к объекту",
                            27: "Этот метод недоступен ключу сообщества; нужен пользовательский ключ",
                            100: "VK отклонил параметры; сверьте контракт и настройки магазина"}
                error = Uncertain if mutation and code == 10 else Rejected
                suffix = str(code) if type(code) is int else "unknown"
                raise error("VK " + suffix + ": " + messages.get(code, "Запрос отклонён; подробности и секреты не выводятся"))
            if "response" not in obj:
                raise (Uncertain if mutation else VKError)("VK не вернул поле response")
            return obj["response"]
        raise VKError("Лимит частоты запросов VK")

    def upload(self, kind, content, filename, group_id, main_photo=True):
        market = kind == "photo.product"
        params = {"group_id": group_id}
        if market:
            params["main_photo"] = bool(main_photo)
        server = self.call("photos.getMarketUploadServer" if market else "photos.getWallUploadServer", params)
        url = server.get("upload_url", "")
        host = urlsplit(url).hostname or ""
        if not any(host == suffix or host.endswith("." + suffix) for suffix in ("vk.com", "vk.ru", "userapi.com", "vk-cdn.net")):
            raise VKError("Сервер загрузки не принадлежит разрешённым доменам VK")
        boundary = "bamboo" + secrets.token_hex(20)
        mime = "image/png" if content.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"
        # Имя из локального пути не пересылается: нет инъекции в multipart-заголовок.
        fixed_name = "upload.png" if mime == "image/png" else "upload.jpg"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"{fixed_name}\"\r\nContent-Type: {mime}\r\n\r\n".encode()
                + content + f"\r\n--{boundary}--\r\n".encode())
        uploaded = self.transport(url, body, "multipart/form-data; boundary=" + boundary, mutation=True)
        required = ("server", "photo", "hash")
        if not all(key in uploaded and uploaded[key] is not None for key in required):
            raise Uncertain("Сервер загрузки не вернул параметры сохранения")
        save = {key: uploaded[key] for key in (*required, "crop_data", "crop_hash") if key in uploaded}
        save["group_id"] = group_id
        result = self.call("photos.saveMarketPhoto" if market else "photos.saveWallPhoto", save, mutation=True)
        if not isinstance(result, list) or not result or not isinstance(result[0], dict) or type(result[0].get("id")) is not int:
            raise Uncertain("Фотография могла сохраниться, но её ID не получен")
        photo = result[0]
        if type(photo.get("owner_id")) is not int:
            raise Uncertain("Не получен владелец фотографии")
        return {"photo_id": photo["id"], "attachment": f"photo{photo['owner_id']}_{photo['id']}"}
