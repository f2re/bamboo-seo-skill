"""HTTPS JSON/bytes transport: без перенаправления секретов и скрытых POST-повторов."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

from . import __version__
from .core import BambooError
from .quality import http_url


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def secret(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BambooError(f"Не задана переменная окружения {name}")
    return value


def request(url: str, *, payload: Any = None, headers: dict | None = None,
            method: str | None = None, readonly: bool = False) -> Any:
    http_url(url, https_only=True)
    data = payload if isinstance(payload, bytes) else (None if payload is None else json.dumps(payload).encode())
    hdr = {"Accept": "application/json", "User-Agent": f"BambooSEO/{__version__}"}
    if data is not None and not isinstance(payload, bytes):
        hdr["Content-Type"] = "application/json; charset=utf-8"
    hdr.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdr, method=method)
    for attempt in range(3 if readonly else 1):
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
                body = response.read(16 * 1024 * 1024 + 1)
                if len(body) > 16 * 1024 * 1024:
                    raise BambooError("Ответ API превышает допустимый размер")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            if readonly and exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            # Не включать response body, токен, query-string или Authorization в сообщение.
            raise BambooError(f"API: HTTP {exc.code}; проверьте доступ, URL и лимиты") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BambooError("Сеть недоступна/тайм-аут. Для записи результат может быть неизвестен; не повторяйте вслепую") from None
        except (ValueError, UnicodeError):
            raise BambooError("API вернул не JSON; проверьте адрес API") from None
