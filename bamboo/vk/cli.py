"""Команды владельца и агента. Авторизация выполняется только локально."""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from bamboo.core import BambooError
from . import auth
from .common import VKError, config, dumps, local, loads, read, safe
from .registry import REGISTRY
from .store import Store, initialize


def add_parser(sub):
    parser = sub.add_parser("vk", help="Управление магазином ВК через утверждённые планы")
    actions = parser.add_subparsers(dest="vk_action", required=True)
    init = actions.add_parser("init", help="Локальная настройка без сети")
    init.add_argument("--community", required=True)
    init.add_argument("--client-id")
    init.add_argument("--redirect-uri", default="http://localhost/vk/callback")
    for name in ("status", "operations", "check", "sync", "login", "auth-start", "auth-finish", "token-import", "mcp", "history"):
        actions.add_parser(name)
    command = actions.add_parser("read")
    command.add_argument("operation", choices=sorted(k for k, v in REGISTRY.items() if not v["write"]))
    command.add_argument("--params", default="{}")
    command = actions.add_parser("plan", help="Подготовить план по снимку, без сети")
    command.add_argument("--file", required=True)
    command = actions.add_parser("plan-post", help="План публикации утверждённого контент-пакета")
    command.add_argument("slug")
    command.add_argument("--request", required=True, help="Исходное поручение пользователя")
    command.add_argument("--publish-date", type=int)
    command.add_argument("--format", choices=["vk", "article"], default="vk", dest="fmt", help="Формат контента: vk или article")
    command = actions.add_parser("publish", help="Опубликовать подготовленный пост по прямому поручению без повторной анкеты")
    command.add_argument("slug")
    command.add_argument("--request", required=True)
    command.add_argument("--execute", action="store_true")
    command.add_argument("--publish-date", type=int)
    for name in ("show", "reconcile"):
        actions.add_parser(name).add_argument("plan_id")
    command = actions.add_parser("apply", help="Без --execute ничего не отправляет")
    command.add_argument("plan_id")
    command.add_argument("--confirm")
    command.add_argument("--execute", action="store_true")
    return parser


def status(root):
    cfg_path = local(root, "config.json")
    return {"configured": cfg_path.exists(), "configuration": config(root) if cfg_path.exists() else None,
            "token_present": bool(os.environ.get("BAMBOO_VK_ACCESS_TOKEN")) or auth.credential_path(root).exists(),
            "network_called": False, "authorization": "not_checked", "next": "vk check"}


def run(args):
    root = args.workspace.resolve()
    action = args.vk_action
    if action == "init":
        return initialize(root, args.community, args.client_id, args.redirect_uri)
    if action == "operations":
        return {"api_version": "5.199", "operations": REGISTRY,
                "limits": "Не поддерживаются платежи, смена владельца/пароля, личные сообщения, произвольные методы и настройки без публичного API"}
    if action == "status":
        return status(root)
    if action in ("login", "auth-start"):
        result = auth.start(root, open_browser=action == "login")
        if action == "auth-start":
            return result
        print(result["authorize_url"], file=sys.stderr)
        print(result["next"], file=sys.stderr)
        return auth.finish(root, getpass.getpass("Адрес возврата (ввод скрыт): "))
    if action == "auth-finish":
        return auth.finish(root, getpass.getpass("Полный адрес localhost (ввод скрыт): "))
    if action == "token-import":
        return auth.import_token(root, getpass.getpass("Пользовательский ключ VK API (ввод скрыт): "))
    if action == "history":
        path = local(root, "receipts")
        return {"receipts": [{"plan_id": p.stem, "statuses": [r["status"] for r in read(p)["actions"]]} for p in sorted(path.glob("*.json"))]}
    store = Store(root)
    if action == "check":
        return store.check()
    if action == "sync":
        return store.sync()
    if action == "read":
        return {"response": store.read_operation(args.operation, loads(args.params)), "source": "VK API", "untrusted_content": True}
    if action == "plan":
        return store.plan(read(safe(root, args.file)))
    if action == "plan-post":
        return store.plan_post(args.slug, args.request, args.publish_date, getattr(args, "fmt", "vk"))
    if action == "publish":
        from bamboo.authorization import publish_vk
        from bamboo.core import lock
        with lock(root):
            return publish_vk(store, args.slug, args.request, args.execute, args.publish_date)
    if action == "show":
        return store.show(args.plan_id)
    if action == "apply":
        return store.apply(args.plan_id, args.confirm, args.execute)
    if action == "reconcile":
        return store.reconcile(args.plan_id)
    raise VKError("Неизвестная команда VK")


def dispatch(args):
    try:
        return run(args)
    except (VKError, BambooError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        text = str(exc) if isinstance(exc, (VKError, BambooError)) else "Некорректные данные или файловая ошибка"
        return {"ok": False, "error": text}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bamboo VK Store")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    add_parser(parser.add_subparsers(dest="command", required=True))
    raw = list(argv if argv is not None else sys.argv[1:])
    # Самостоятельный запуск: python -m bamboo.vk [--workspace PATH] vk ...
    args = parser.parse_args(raw)
    if args.vk_action == "mcp":
        from .mcp import serve
        return serve(args.workspace)
    result = dispatch(args)
    print(dumps(result))
    return 1 if result.get("ok") is False else 0
