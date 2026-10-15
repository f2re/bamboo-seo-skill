"""Yandex Object Storage (S3 API) с подписью AWS SigV4 на стандартной библиотеке.

Бакет остаётся закрытым: Instagram получает временную подписанную ссылку на чтение.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener
from urllib.error import HTTPError, URLError

from ..vk.transport import NoRedirect
from .errors import IGError, Uncertain

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, text: str) -> bytes:
    return hmac.new(key, text.encode(), hashlib.sha256).digest()


def signing_key(secret: str, day: str, region: str, service: str = "s3") -> bytes:
    key = _hmac(("AWS4" + secret).encode(), day)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def canonical_query(params: dict) -> str:
    return "&".join(f"{quote(k, safe='-_.~')}={quote(str(v), safe='-_.~')}" for k, v in sorted(params.items()))


class Storage:
    def __init__(self, key_id: str, secret: str, bucket: str, endpoint: str = "https://storage.yandexcloud.net",
                 region: str = "ru-central1", opener=None, clock=time.time):
        url = urlsplit(endpoint)
        if url.scheme != "https" or not url.hostname or url.path not in ("", "/"):
            raise IGError("Адрес хранилища: только https-хост без пути")
        if not bucket or "/" in bucket or "." in bucket:
            raise IGError("Имя бакета: без точек и косых черт (нужен https path-style)")
        self.key_id, self.secret, self.bucket = key_id, secret, bucket
        self.host, self.region = url.hostname, region
        self.opener = opener or build_opener(NoRedirect()).open
        self.clock = clock

    def path(self, key: str) -> str:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise IGError("Некорректный ключ объекта")
        return "/" + quote(self.bucket, safe="") + "/" + quote(key, safe="/-_.~")

    def _scope(self, stamp: str) -> str:
        return f"{stamp[:8]}/{self.region}/s3/aws4_request"

    def _signature(self, method: str, path: str, query: str, headers: dict, payload_hash: str, stamp: str) -> tuple[str, str]:
        names = sorted(k.lower() for k in headers)
        canonical_headers = "".join(f"{k}:{str(headers[next(h for h in headers if h.lower() == k)]).strip()}\n" for k in names)
        signed = ";".join(names)
        request = "\n".join([method, path, query, canonical_headers, signed, payload_hash])
        to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, self._scope(stamp), hashlib.sha256(request.encode()).hexdigest()])
        key = signing_key(self.secret, stamp[:8], self.region)
        return hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest(), signed

    def _stamp(self) -> str:
        return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))

    def presign_get(self, key: str, expires: int = 3600, host: str | None = None, path: str | None = None) -> str:
        """Временная ссылка на чтение. host/path задаются только тестовым вектором AWS."""
        if not 1 <= expires <= 604800:
            raise IGError("Срок ссылки: от 1 секунды до 7 дней")
        host, path, stamp = host or self.host, path or self.path(key), self._stamp()
        params = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{self.key_id}/{self._scope(stamp)}",
                  "X-Amz-Date": stamp, "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": "host"}
        query = canonical_query(params)
        signature, _ = self._signature("GET", path, query, {"host": host}, "UNSIGNED-PAYLOAD", stamp)
        return f"https://{host}{path}?{query}&X-Amz-Signature={signature}"

    def _send(self, method: str, key: str, body: bytes = b"", content_type: str | None = None, mutation: bool = True) -> int:
        path, stamp = self.path(key), self._stamp()
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        headers = {"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": stamp}
        if content_type:
            headers["content-type"] = content_type
        signature, signed = self._signature(method, path, "", headers, payload_hash, stamp)
        headers["Authorization"] = (f"AWS4-HMAC-SHA256 Credential={self.key_id}/{self._scope(stamp)}, "
                                    f"SignedHeaders={signed}, Signature={signature}")
        outgoing = {k: v for k, v in headers.items() if k != "host"}
        request = Request(f"https://{self.host}{path}", data=body if method == "PUT" else None, headers=outgoing, method=method)
        try:
            with self.opener(request, timeout=60) as response:
                response.read(65536)
                return response.status
        except HTTPError as exc:
            if exc.code == 404 and method in ("HEAD", "DELETE"):
                return 404
            messages = {403: "Хранилище отклонило ключ или права (нужна роль storage.editor на бакет)",
                        404: "Бакет не найден"}
            raise IGError("Хранилище: " + messages.get(exc.code, f"HTTP {exc.code}")) from None
        except (URLError, OSError) as exc:
            raise (Uncertain if mutation else IGError)("Хранилище не ответило") from exc

    def put(self, key: str, body: bytes, content_type: str = "video/mp4") -> None:
        # PUT идемпотентен: повтор с тем же ключом и содержимым безопасен.
        self._send("PUT", key, body, content_type)

    def exists(self, key: str) -> bool:
        return self._send("HEAD", key, mutation=False) == 200

    def delete(self, key: str) -> None:
        self._send("DELETE", key)
