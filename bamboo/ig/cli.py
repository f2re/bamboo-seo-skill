"""Команды Instagram. Ключи вводятся только локально, скрытым вводом."""
from __future__ import annotations

import getpass

from ..vk.common import VKError
from . import secrets
from .publisher import Publisher, config, initialize, local


def add_parser(sub):
    parser = sub.add_parser("ig", help="Публикация Reels в Instagram из утверждённых пакетов")
    actions = parser.add_subparsers(dest="ig_action", required=True)
    init = actions.add_parser("init", help="Локальная настройка без сети")
    init.add_argument("--bucket", required=True, help="Бакет Yandex Object Storage")
    init.add_argument("--prefix", default="bamboo-ig/")
    for name in ("status", "check", "token-import", "storage-key-import"):
        actions.add_parser(name)
    command = actions.add_parser("plan-post", help="План публикации утверждённого пакета, без сети")
    command.add_argument("slug")
    command.add_argument("--request", required=True, help="Поручение владельца")
    actions.add_parser("show").add_argument("plan_id")
    command = actions.add_parser("apply", help="Без --execute ничего не отправляет")
    command.add_argument("plan_id")
    command.add_argument("--confirm")
    command.add_argument("--execute", action="store_true")
    return parser


def status(root):
    configured = local(root, "config.json").exists()
    data = secrets.load(root)
    return {"configured": configured, "configuration": config(root) if configured else None,
            "token_present": bool(data.get("access_token")), "storage_keys_present": bool(data.get("storage_key_id")),
            "network_called": False, "next": "ig check"}


def dispatch(args):
    try:
        return run(args)
    except (VKError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        text = str(exc) if isinstance(exc, VKError) else "Некорректные данные или файловая ошибка"
        return {"ok": False, "error": text}


def run(args):
    root = args.workspace.resolve()
    action = args.ig_action
    if action == "init":
        return initialize(root, args.bucket, args.prefix)
    if action == "status":
        return status(root)
    if action == "token-import":
        return secrets.import_token(root, getpass.getpass("Ключ доступа Instagram (ввод скрыт): "))
    if action == "storage-key-import":
        key_id = getpass.getpass("Идентификатор статического ключа Yandex (ввод скрыт): ")
        return secrets.import_storage(root, key_id, getpass.getpass("Секретный ключ Yandex (ввод скрыт): "))
    publisher = Publisher(root)
    if action == "check":
        return publisher.check()
    if action == "plan-post":
        return publisher.plan_post(args.slug, args.request)
    if action == "show":
        return publisher.show(args.plan_id)
    return publisher.apply(args.plan_id, args.confirm, args.execute)
