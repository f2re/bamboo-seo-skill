"""Ошибки Instagram-модуля: только подготовленные сообщения, без ответов сервера и секретов."""
from __future__ import annotations

from ..vk.common import VKError


class IGError(VKError):
    """Ошибка, которую можно показать владельцу."""


class Uncertain(IGError):
    """Сервер мог выполнить операцию; перед повтором нужна сверка."""


class Rejected(IGError):
    """Instagram или хранилище явно отклонили запрос."""
