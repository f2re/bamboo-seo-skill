"""Файлы проекта, безопасные пути, снимки и блокировки."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import __version__

FORMATS = {"article": (800, 1500, "words"), "vk": (800, 1500, "chars"),
           "card": (300, 500, "chars"), "stories": (30, 60, "lines"),
           "carousel": (0, 0, "none"), "faq": (0, 0, "none")}
CHECKS = ("facts", "culture", "voice", "photos", "privacy", "clickbait", "cta")
DEFAULTS = {
    "schema_version": 1, "brand": "Bamboo Pottery", "language": "ru",
    "site_url": None, "shop_url": None, "wordpress_url": None,
    "quality": {"strict_lengths": False, "require_product_photos": True},
    "analytics": {"gsc_property": None, "yandex_user_id": None,
                  "yandex_host_id": None, "min_impressions": 100},
}


class BambooError(Exception):
    """Ошибка, которую можно показать пользователю без трассировки и секретов."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) or len(value) > 80:
        raise BambooError("Идентификатор: латинские строчные буквы, цифры, дефисы; до 80 знаков")
    return value


def safe(root: Path, relative: str | Path) -> Path:
    """Не разрешать абсолютные пути, .. и символические ссылки даже внутри проекта."""
    root = root.resolve()
    p = Path(relative)
    if p.is_absolute() or ".." in p.parts or "\\" in str(relative):
        raise BambooError(f"Небезопасный относительный путь: {relative}")
    out = root
    for part in p.parts:
        out = out / part
        if out.is_symlink():
            raise BambooError(f"Символическая ссылка запрещена: {relative}")
    if not out.resolve().is_relative_to(root):
        raise BambooError("Путь выходит за пределы проекта")
    return out


def _pairs(items: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, val in items:
        if key in result:
            raise ValueError(f"Повторный JSON-ключ: {key}")
        result[key] = val
    return result


def read_json(path: Path) -> Any:
    if path.is_symlink():
        raise BambooError("Чтение JSON через символическую ссылку запрещено")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_pairs,
                          parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    except (OSError, ValueError) as exc:
        raise BambooError(f"Не удалось прочитать JSON: {path.name} ({type(exc).__name__})") from exc


def write_text(path: Path, text: str) -> None:
    if path.is_symlink():
        raise BambooError("Запись через символическую ссылку запрещена")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".bamboo-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path: Path, data: Any) -> None:
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_digest(path: Path) -> str:
    if path.is_symlink():
        raise BambooError("Хеширование символической ссылки запрещено")
    return digest(path.read_bytes())


@contextmanager
def lock(root: Path, name: str = "workspace") -> Iterator[None]:
    target = safe(root, f".bamboo-locks/{slug(name)}.lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise BambooError(f"Операция уже выполняется: {target.name}. После аварии проверьте PID в lock-файле") from exc
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(json.dumps({"pid": os.getpid(), "created_at": now()}))
        yield
    finally:
        target.unlink(missing_ok=True)


def config(root: Path) -> dict:
    obj = read_json(safe(root, "bamboo.json"))
    if not isinstance(obj, dict) or obj.get("schema_version") != 1:
        raise BambooError("bamboo.json: требуется schema_version=1")
    if not isinstance(obj.get("quality"), dict) or not isinstance(obj.get("analytics"), dict):
        raise BambooError("bamboo.json: отсутствуют объекты quality/analytics")
    for key in ("strict_lengths", "require_product_photos"):
        if key in obj["quality"] and not isinstance(obj["quality"][key], bool):
            raise BambooError(f"quality.{key}: требуется true/false, не строка")
    minimum = obj["analytics"].get("min_impressions", 100)
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or minimum < 0:
        raise BambooError("analytics.min_impressions: нужно неотрицательное число")
    return obj


def init(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    with lock(root):
        path = safe(root, "bamboo.json")
        if path.exists():
            return {"status": "exists", "workspace": str(root.resolve())}
        write_json(path, DEFAULTS)
        for folder in ("content/products", "content/media", "content/voice", "content/jobs", "analytics"):
            safe(root, folder).mkdir(parents=True, exist_ok=True)
        write_text(safe(root, "content/voice/README.md"),
                   "Добавьте сюда подтверждённые публичные тексты мастера в .md.\n"
                   "Не добавляйте переписку покупателей и непубличные данные.\n")
    return {"status": "created", "workspace": str(root.resolve())}


def job_path(root: Path, name: str) -> Path:
    return safe(root, f"content/jobs/{slug(name)}")


def new_job(root: Path, name: str, topic: str, formats: list[str],
            products: list[str], audience: str = "A") -> dict:
    config(root)
    slug(name)
    if not topic.strip() or not formats or len(formats) != len(set(formats)) or set(formats) - FORMATS.keys():
        raise BambooError("Нужны тема и уникальные форматы: " + ", ".join(FORMATS))
    if audience not in ("A", "B", "C", "mixed"):
        raise BambooError("Аудитория: A, B, C или mixed")
    for product in products:
        read_json(safe(root, f"content/products/{slug(product)}.json"))
    target = job_path(root, name)
    with lock(root):
        if target.exists():
            raise BambooError("Такая задача уже существует; прежние материалы не перезаписаны")
        target.mkdir(parents=True)
        write_json(target / "brief.json", {
            "schema_version": 1, "slug": name, "topic": topic, "audience": audience,
            "formats": formats, "product_ids": products, "intent": "",
            "master_notes": "", "master_notes_public": False,
            "demand": {"status": "unknown", "evidence": []}, "created_at": now()})
        write_json(target / "sources.json", [])
        write_json(target / "claims.json", [])
        write_json(target / "pack.json", {
            "schema_version": 1, "title": topic, "description": "",
            "formats": {fmt: {"text": "", "cta": "", "claims": []} for fmt in formats},
            "photos": []})
        write_text(target / "research.md", "# Исследование\n\nНужны фактура мастера и проверяемые источники.\n")
        write_text(target / "variants.md", "# Варианты\n\nДо 3 вариантов запрошенного формата; не выдумывать факты.\n")
    return {"job": name, "path": str(target), "status": "brief"}


def snapshot(root: Path, name: str) -> str:
    """Связывает утверждение с конфигом, фактами, файлами источников, товарами и фото."""
    job = job_path(root, name)
    brief = read_json(job / "brief.json")
    pack = read_json(job / "pack.json")
    sources = read_json(job / "sources.json")
    paths = {safe(root, "bamboo.json")}
    paths.update(job / f for f in ("brief.json", "pack.json", "sources.json", "claims.json"))
    for product in brief.get("product_ids", []):
        paths.add(safe(root, f"content/products/{slug(product)}.json"))
    for source in sources:
        if source.get("path"):
            paths.add(safe(root, source["path"]))
    for photo in pack.get("photos", []):
        paths.add(safe(root, photo["path"]))
    for path in safe(root, "content/voice").glob("*.md"):
        paths.add(safe(root, path.relative_to(root.resolve())))
    for filename in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
        path = safe(root, filename)
        if path.is_file():
            paths.add(path)
    hashes = {p.relative_to(root.resolve()).as_posix(): file_digest(p) for p in sorted(paths)}
    # Изменение кода и канонических политик также требует повторного утверждения.
    engine = Path(__file__).resolve().parent.parent
    for folder in ("bamboo", "docs", "skills", "agents"):
        for path in sorted((engine / folder).rglob("*")):
            if path.is_file() and path.suffix in (".py", ".md") and "__pycache__" not in path.parts:
                hashes["engine/" + path.relative_to(engine).as_posix()] = file_digest(path)
    hashes["engine_version"] = __version__
    return digest(json.dumps(hashes, sort_keys=True).encode())


def approval_token(root: Path, name: str) -> str:
    return f"{name}@{snapshot(root, name)}"
