"""Instagram API with Instagram Login (graph.instagram.com): ключ, профиль, контейнеры Reels."""
from __future__ import annotations

import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from ..vk.common import VKError, loads
from ..vk.transport import NoRedirect
from .errors import IGError, Rejected, Uncertain

HOST = "https://graph.instagram.com"
REFRESH_AFTER = 24 * 3600          # ключ можно продлить не раньше чем через сутки после выдачи
REFRESH_WHEN_LEFT = 30 * 24 * 3600  # продлевать, когда осталось меньше 30 дней


def request_json(method: str, url: str, data: bytes | None = None, mutation: bool = False, opener=None) -> dict:
    if not url.startswith(HOST + "/"):
        raise IGError("Разрешён только graph.instagram.com")
    req = Request(url, data=data, method=method, headers={"User-Agent": "Bamboo-IG/1.0",
                                                          **({"Content-Type": "application/x-www-form-urlencoded"} if data else {})})
    try:
        with (opener or build_opener(NoRedirect()).open)(req, timeout=60) as response:
            raw = response.read(2 * 1024 * 1024)
    except HTTPError as exc:
        raw = exc.read(65536)
    except (URLError, OSError) as exc:
        raise (Uncertain if mutation else IGError)("Instagram не ответил; запись не повторяется вслепую") from exc
    try:
        result = loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, VKError) as exc:
        raise (Uncertain if mutation else IGError)("Instagram вернул некорректный ответ") from exc
    if not isinstance(result, dict):
        raise (Uncertain if mutation else IGError)("Instagram вернул некорректный ответ")
    if "error" in result:
        error = result["error"] if isinstance(result["error"], dict) else {}
        code = error.get("code")
        messages = {190: "Ключ Instagram недействителен или истёк: выполните ig token-import",
                    10: "Приложению не выдано право instagram_business_content_publish",
                    200: "Нет прав на операцию; проверьте права приложения",
                    4: "Лимит запросов Instagram; повторите позже",
                    9: "Лимит публикаций Instagram за сутки исчерпан",
                    100: "Instagram отклонил параметры (ссылку на видео, подпись или формат)"}
        raise Rejected("Instagram " + (str(code) if type(code) is int else "error") + ": "
                       + messages.get(code, "запрос отклонён; подробности и ключ не выводятся"))
    return result


class Graph:
    def __init__(self, token: str, version: str = "v25.0", opener=None, sleep=time.sleep):
        if not version.startswith("v"):
            raise IGError("Версия API вида v25.0")
        self.token, self.version, self.opener, self.sleep = token, version, opener, sleep

    def _url(self, path: str, params: dict | None = None) -> str:
        query = urlencode({**(params or {}), "access_token": self.token})
        return f"{HOST}/{self.version}/{path}?{query}"

    def get(self, path: str, **params) -> dict:
        return request_json("GET", self._url(path, params), opener=self.opener)

    def post(self, path: str, **params) -> dict:
        body = urlencode({**params, "access_token": self.token}).encode()
        return request_json("POST", f"{HOST}/{self.version}/{path}", body, mutation=True, opener=self.opener)

    def me(self) -> dict:
        result = self.get("me", fields="user_id,username,account_type")
        if not str(result.get("user_id", "")).isdigit():
            raise IGError("Instagram не вернул user_id профессионального аккаунта")
        return result

    def publishing_limit(self, user_id: str) -> dict:
        result = self.get(f"{user_id}/content_publishing_limit", fields="quota_usage,config")
        return (result.get("data") or [{}])[0]

    def create_reel(self, user_id: str, video_url: str, caption: str, share_to_feed: bool = True) -> str:
        result = self.post(f"{user_id}/media", media_type="REELS", video_url=video_url, caption=caption,
                           share_to_feed="true" if share_to_feed else "false")
        if not str(result.get("id", "")).isdigit():
            raise Uncertain("Контейнер мог создаться, но ID не получен")
        return result["id"]

    def status(self, container_id: str) -> str:
        return str(self.get(container_id, fields="status_code").get("status_code", ""))

    def wait(self, container_id: str, timeout: int = 300, every: int = 15) -> str:
        waited = 0
        while True:
            status = self.status(container_id)
            if status != "IN_PROGRESS" or waited >= timeout:
                return status
            self.sleep(every)
            waited += every

    def publish(self, user_id: str, container_id: str) -> str:
        result = self.post(f"{user_id}/media_publish", creation_id=container_id)
        if not str(result.get("id", "")).isdigit():
            raise Uncertain("Публикация могла пройти, но ID записи не получен")
        return result["id"]

    def media(self, media_id: str) -> dict:
        return self.get(media_id, fields="id,permalink,media_product_type,timestamp,caption")

    def recent(self, user_id: str) -> list:
        return self.get(f"{user_id}/media", fields="id,permalink,caption,timestamp", limit=10).get("data", [])


def refresh(token: str, opener=None) -> dict:
    url = f"{HOST}/refresh_access_token?" + urlencode({"grant_type": "ig_refresh_token", "access_token": token})
    result = request_json("GET", url, opener=opener)
    if not isinstance(result.get("access_token"), str) or type(result.get("expires_in")) is not int:
        raise IGError("Instagram не продлил ключ")
    return result
