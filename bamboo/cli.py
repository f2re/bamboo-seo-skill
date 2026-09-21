"""Единая командная строка. Сеть и публикация только по явной команде."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

from . import __version__
from . import analytics, publishing, quality
from .core import (BambooError, approval_token, config, file_digest, init, job_path,
                   lock, new_job, read_json, safe, snapshot)
from .install import SYSTEMS, install, uninstall


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bamboo Pottery: фактура → контент → проверка → публикация")
    p.add_argument("--workspace", type=Path, default=Path.cwd(), help="Корень проекта (до подкоманды)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Создать локальные данные без перезаписи")
    sub.add_parser("doctor", help="Проверить среду без сети и вывода секретов")
    n = sub.add_parser("new", help="Создать незаполненное задание")
    n.add_argument("slug")
    n.add_argument("--topic", required=True)
    n.add_argument("--audience", choices=["A", "B", "C", "mixed"], default="A")
    n.add_argument("--formats", default="article,vk,card,stories")
    n.add_argument("--products", default="")
    for name in ("validate", "status", "review-template", "export", "wp-reconcile"):
        sub.add_parser(name).add_argument("slug")
    a = sub.add_parser("approve", help="Утвердить вручную проверенную конкретную версию")
    a.add_argument("slug")
    a.add_argument("--confirm", required=True, help="Полный токен slug@sha256 из status")
    a = sub.add_parser("publish", help="По умолчанию показать план без записи и сети")
    a.add_argument("slug")
    a.add_argument("--channel", choices=["static", "wordpress"], required=True)
    a.add_argument("--execute", action="store_true")
    a.add_argument("--live", action="store_true", help="WordPress publish вместо draft")
    a.add_argument("--confirm", help="Обязателен для --live")
    a = sub.add_parser("hash-file", help="SHA256 файла для паспорта фото")
    a.add_argument("path", type=Path)
    a = sub.add_parser("analytics-import")
    a.add_argument("--file", required=True, type=Path)
    a = sub.add_parser("analytics-pull", help="Прочитать API; конфиг и OAuth задаёт владелец")
    a.add_argument("--provider", required=True, choices=["google", "yandex"])
    a.add_argument("--start", required=True)
    a.add_argument("--end", required=True)
    a = sub.add_parser("analytics-report")
    a.add_argument("--end", default=date.today().isoformat())
    a.add_argument("--days", type=int, default=7)
    a = sub.add_parser("install", help="Установить в отдельный проект; чужие файлы сохраняются")
    a.add_argument("--target", required=True, type=Path)
    a.add_argument("--systems", default="all")
    a.add_argument("--dry-run", action="store_true")
    a = sub.add_parser("uninstall")
    a.add_argument("--dry-run", action="store_true")
    return p


def doctor(root: Path) -> dict:
    return {"version": __version__, "python": sys.version.split()[0],
            "workspace": str(root), "config_exists": safe(root, "bamboo.json").exists(),
            "runtime_executables": {n: bool(shutil.which(n)) for n in ("codex", "claude", "antigravity")},
            "native_directories": {n: safe(root, n).is_dir() for n in
                                   (".agents/skills", ".codex/agents", ".claude/skills", ".claude/agents", ".agents/agents")},
            "credentials_present": {n: bool(os.environ.get(n)) for n in
                ("BAMBOO_WP_USER", "BAMBOO_WP_APP_PASSWORD", "BAMBOO_GSC_ACCESS_TOKEN",
                 "BAMBOO_GSC_REFRESH_TOKEN", "BAMBOO_YANDEX_TOKEN")},
            "note": "Наличие файлов или переменных не доказывает авторизацию либо запуск ИИ. Сетевой проверки не было."}


def dispatch(a: argparse.Namespace) -> dict:
    root = a.workspace.resolve()
    cmd = a.command
    if cmd == "init":
        return init(root)
    if cmd == "doctor":
        return doctor(root)
    if cmd == "install":
        systems = SYSTEMS if a.systems == "all" else set(a.systems.split(","))
        return install(Path(__file__).resolve().parent.parent, a.target, systems, a.dry_run)
    if cmd == "uninstall":
        return uninstall(root, a.dry_run)
    if cmd == "hash-file":
        return {"sha256": file_digest(a.path.resolve())}
    config(root)
    if cmd == "new":
        return new_job(root, a.slug, a.topic, a.formats.split(","),
                       [x for x in a.products.split(",") if x], a.audience)
    if cmd == "validate":
        return quality.validate(root, a.slug)
    if cmd == "status":
        report = quality.validate(root, a.slug)
        result = {"validation": report}
        if report["ok"]:
            result["confirmation_token"] = approval_token(root, a.slug)
            try:
                quality.require_approval(root, a.slug)
                result["approval"] = "current"
            except BambooError:
                result["approval"] = "missing_or_stale"
        return result
    if cmd == "publish" and not a.execute:
        return {"dry_run": True, "channel": a.channel,
                "target_status": "publish" if a.live else ("draft" if a.channel == "wordpress" else "built_locally"),
                "validation": quality.validate(root, a.slug), "network_called": False,
                "note": "Для выполнения нужны актуальное утверждение и --execute"}
    with lock(root):
        if cmd == "review-template":
            return quality.review_template(root, a.slug)
        if cmd == "approve":
            return quality.approve(root, a.slug, a.confirm)
        if cmd == "export":
            return publishing.export(root, a.slug)
        if cmd == "publish":
            if a.live and a.channel != "wordpress":
                raise BambooError("--live применим только к WordPress")
            if a.live and a.confirm != approval_token(root, a.slug):
                raise BambooError("Для --live нужен --confirm с полным текущим токеном версии")
            return (publishing.publish_static(root, a.slug) if a.channel == "static" else
                    publishing.publish_wordpress(root, a.slug, live=a.live))
        if cmd == "wp-reconcile":
            return publishing.wp_reconcile(root, a.slug)
        if cmd == "analytics-import":
            return analytics.import_csv(root, a.file)
        if cmd == "analytics-pull":
            return analytics.pull(root, a.provider, a.start, a.end)
        if cmd == "analytics-report":
            return analytics.report(root, a.end, a.days)
    raise BambooError("Неизвестная команда")


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        print("Требуется Python 3.10 или новее", file=sys.stderr)
        return 2
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 1 if result.get("ok") is False else 0
    except (BambooError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Не показывать тела HTTP-ответов, токены и трассировки.
        text = str(exc) if isinstance(exc, BambooError) else f"Некорректные данные или файловая ошибка ({type(exc).__name__})"
        print(json.dumps({"error": text}, ensure_ascii=False), file=sys.stderr)
        return 2
