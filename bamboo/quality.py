"""Детерминированные проверки. Не определяет авторство ИИ и не заменяет фактчек."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from .core import (BambooError, CHECKS, FORMATS, approval_token, config, file_digest,
                   job_path, now, read_json, safe, slug, snapshot, write_json)

CLICHES = ("непревзойдённое мастерство", "шедевр эпохи", "истинные ценители прекрасного",
           "воплощение древней мудрости", "энергия глины")
BAIT = (r"вы не поверите", r"шок\w*", r"секрет,? котор", r"гарантирован\w* (?:доход|рост|результат)",
        r"изменит вашу жизнь", r"успейте.{0,20}только сегодня", r"никто не расскажет")
HEALTH = r"(?:лечит|исцеляет|улучшает здоровье|очищает воду|выводит токсины)"
NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:мл|см|мм|кг|г\b|°[CcСс]|₽|руб\w*)", re.I)
MARK = re.compile(r"\[\[([a-z0-9-]+)\]\]")


def http_url(value: str, https_only: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in (("https",) if https_only else ("http", "https")) or not parsed.hostname:
            raise ValueError()
        if parsed.username or parsed.password or any(ord(x) < 32 for x in value):
            raise ValueError()
    except (ValueError, TypeError) as exc:
        raise BambooError("Нужен корректный URL без учётных данных") from exc
    return value


def clean(text: str) -> str:
    return MARK.sub("", text).strip()


def issue(level: str, code: str, message: str) -> dict:
    return {"level": level, "code": code, "message": message}


def lint(text: str) -> list[dict]:
    result = []
    low = text.lower()
    for word in CLICHES:
        if word in low:
            result.append(issue("warning", "cliche", f"Клише: {word}"))
    for pattern in BAIT:
        if re.search(pattern, low):
            result.append(issue("warning", "clickbait", f"Проверить обещание/кликбейт: {pattern}"))
    if re.search(HEALTH, low):
        result.append(issue("warning", "health_claim", "Проверить контекст медицинского заявления; не публиковать неподтверждённое обещание"))
    if re.search(r"\b(?:TODO|TBD|ЗАПОЛНИТЬ)\b|<script\b", text, re.I):
        result.append(issue("error", "placeholder", "Остался шаблон или исполняемый HTML"))
    if re.search(r"(?:sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{15,}|-----BEGIN .*PRIVATE KEY)", text):
        result.append(issue("error", "secret", "В публичном материале похожие на секрет данные"))
    if re.search(r"[\u200b\u200c\u200d\ufeff]", text):
        result.append(issue("warning", "invisible", "Невидимые символы: проверить вставку; это не признак авторства ИИ"))
    return result


def validate(root: Path, name: str) -> dict:
    errors = []
    warnings = []
    counts = {}
    def fail(msg: str) -> None:
        errors.append(issue("error", "validation", msg))
    try:
        cfg = config(root)
        job = job_path(root, name)
        brief, pack, sources, claims = (read_json(job / f) for f in
                                      ("brief.json", "pack.json", "sources.json", "claims.json"))
        if not isinstance(brief, dict) or not isinstance(pack, dict) or not isinstance(sources, list) or not isinstance(claims, list):
            raise BambooError("brief/pack должны быть объектами; sources/claims — массивами")
        if not isinstance(pack.get("photos"), list) or not isinstance(pack.get("formats"), dict):
            raise BambooError("pack требует photos-массив и formats-объект")
        if not isinstance(pack.get("title"), str) or not isinstance(pack.get("description"), str):
            raise BambooError("title и description должны быть строками")
        if brief.get("schema_version") != 1 or pack.get("schema_version") != 1:
            fail("Неподдерживаемая схема brief/pack")
        if brief.get("slug") != name or not brief.get("intent", "").strip():
            fail("Нужны совпадающий slug и поисковое/покупательское намерение intent")
        if brief.get("demand", {}).get("status") not in ("measured", "proxy", "unknown"):
            fail("demand.status должен быть measured/proxy/unknown")
        if brief.get("demand", {}).get("status") in ("measured", "proxy") and not brief["demand"].get("evidence"):
            fail("Измеренный/косвенный спрос требует указания evidence")
        if not brief.get("master_notes", "").strip() or brief.get("master_notes_public") is not True:
            fail("Нужны фактура мастера и master_notes_public=true (разрешение на её использование)")
        if not pack.get("title", "").strip():
            fail("Нет заголовка")
        wanted = brief.get("formats", [])
        if not wanted or set(wanted) - FORMATS.keys() or set(pack.get("formats", {})) != set(wanted):
            fail("Набор форматов pack должен совпадать с brief")
        src = {}
        for item in sources:
            sid = slug(item["id"])
            if sid in src:
                fail(f"Повтор источника: {sid}")
            src[sid] = item
            if item.get("visibility") != "public" or not item.get("title", "").strip():
                fail(f"Источник {sid}: нужны публичность и название")
            date.fromisoformat(item["verified_at"])
            if item.get("kind") == "web":
                http_url(item.get("url", ""))
            elif item.get("kind") in ("product", "master"):
                path = safe(root, item["path"])
                if not path.is_file():
                    fail(f"Нет локального источника {sid}")
                if item.get("kind") == "product" and not item["path"].startswith("content/products/"):
                    fail(f"Источник товара {sid} должен находиться в content/products")
            else:
                fail(f"Источник {sid}: kind должен быть web/product/master")
        registry = {}
        for claim in claims:
            cid = slug(claim["id"])
            if cid in registry:
                fail(f"Повтор утверждения: {cid}")
            registry[cid] = claim
            if claim.get("status") != "verified" or not claim.get("checked_by", "").strip():
                fail(f"Утверждение {cid} не проверено")
            if not claim.get("text", "").strip() or not claim.get("source_ids"):
                fail(f"Утверждение {cid}: нет текста/источников")
            if set(claim.get("source_ids", [])) - src.keys():
                fail(f"Утверждение {cid} ссылается на неизвестный источник")
        product_ids = brief.get("product_ids", [])
        for pid in product_ids:
            product = read_json(safe(root, f"content/products/{slug(pid)}.json"))
            if product.get("id") != pid or product.get("confirmed") is not True:
                fail(f"Паспорт товара {pid} не подтверждён мастером")
        all_public = [pack.get("title", ""), pack.get("description", "")]
        for fmt, item in pack.get("formats", {}).items():
            text, cta = item["text"], item["cta"]
            if not text.strip() or not cta.strip():
                fail(f"{fmt}: нужны текст и следующий шаг cta")
            used = set(item.get("claims", []))
            if used - registry.keys() or set(MARK.findall(text + cta)) - used:
                fail(f"{fmt}: неизвестное или незарегистрированное утверждение")
            if not used:
                fail(f"{fmt}: должен опираться хотя бы на один подтверждённый факт")
            supported = " ".join(registry[c]["text"] for c in used if c in registry)
            norm = lambda s: re.sub(r"\s+", "", s).replace(",", ".").lower()
            allowed = {norm(x) for x in NUMBER.findall(supported)}
            for value in NUMBER.findall(text + " " + cta):
                if norm(value) not in allowed:
                    fail(f"{fmt}: числовая характеристика без связанного факта: {value}")
            full = clean(text + "\n" + cta)
            lower, upper, mode = FORMATS.get(fmt, (0, 0, "none"))
            lengths = ([len(x.strip()) for x in clean(text).splitlines() if x.strip()] if mode == "lines"
                       else [len(re.findall(r"\b[\w-]+\b", full)) if mode == "words" else len(full)])
            counts[fmt] = {"values": lengths, "unit": mode}
            if mode != "none" and any(not lower <= v <= upper for v in lengths):
                msg = issue("error" if cfg["quality"].get("strict_lengths") else "warning", "length",
                            f"{fmt}: {lengths}; ориентир {lower}–{upper} ({mode})")
                (errors if msg["level"] == "error" else warnings).append(msg)
            all_public += [text, cta]
        photographed = set()
        for photo in pack.get("photos", []):
            path = safe(root, photo["path"])
            if not photo["path"].startswith("content/media/") or path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                fail("Фотографии: только jpg/png/webp в content/media")
            if photo.get("origin") != "camera" or photo.get("rights_confirmed") is not True:
                fail("Для товарного пакета нужны реальные фото с подтверждёнными правами")
            if not photo.get("alt", "").strip():
                fail("Фотография без alt")
            if photo.get("sha256") != file_digest(path):
                fail("Фото изменено или не закреплено sha256")
            subjects = set(photo.get("product_ids", []))
            if subjects - set(product_ids):
                fail("Фото приписывает авторство предмету, которого нет в паспортах задачи")
            photographed.update(subjects)
            all_public += [photo.get("alt", ""), photo.get("caption", "")]
        if cfg["quality"].get("require_product_photos", True) and set(product_ids) - photographed:
            fail("Для каждого товара нужна фотография, связанная с его product_id")
        for finding in lint("\n".join(all_public)):
            (errors if finding["level"] == "error" else warnings).append(finding)
        if "article" in wanted and not pack.get("description", "").strip():
            fail("Для статьи нужно description")
    except (BambooError, KeyError, TypeError, ValueError, AttributeError, OSError) as exc:
        fail(f"Неверная структура или отсутствующий файл: {type(exc).__name__}: {exc}")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "counts": counts,
            "note": "Проверка структуры и эвристик, не доказательство истинности и не AI-детектор"}


def review_template(root: Path, name: str) -> dict:
    report = validate(root, name)
    if not report["ok"]:
        raise BambooError("Сначала устраните ошибки validate")
    data = {"schema_version": 1, "content_hash": snapshot(root, name), "reviewer": "",
            "mode": "human", "checks": {k: False for k in CHECKS}, "notes": "",
            "warnings_acknowledged": False, "created_at": now()}
    path = job_path(root, name) / "review.json"
    if path.exists():
        raise BambooError("review.json уже есть; обновите его после повторной проверки и получения нового hash")
    write_json(path, data)
    return data


def approve(root: Path, name: str, confirmation: str) -> dict:
    report = validate(root, name)
    if not report["ok"] or confirmation != approval_token(root, name):
        raise BambooError("Проверка не пройдена или токен подтверждения устарел")
    review = read_json(job_path(root, name) / "review.json")
    if review.get("content_hash") != snapshot(root, name):
        raise BambooError("Редактор проверил другую версию материала")
    if not review.get("reviewer", "").strip() or not review.get("notes", "").strip():
        raise BambooError("Укажите редактора и содержательные замечания")
    if review.get("mode") != "human" or any(review.get("checks", {}).get(k) is not True for k in CHECKS):
        raise BambooError("Нужна человеческая проверка всех разделов")
    if report["warnings"] and review.get("warnings_acknowledged") is not True:
        raise BambooError("Редактор должен рассмотреть предупреждения validate")
    data = {"content_hash": snapshot(root, name), "review_hash": file_digest(job_path(root, name) / "review.json"),
            "reviewer": review["reviewer"], "approved_at": now()}
    write_json(job_path(root, name) / "approval.json", data)
    return data


def require_approval(root: Path, name: str) -> dict:
    report = validate(root, name)
    if not report["ok"]:
        raise BambooError("Контент не прошёл validate")
    job = job_path(root, name)
    data = read_json(job / "approval.json")
    if data.get("content_hash") != snapshot(root, name) or data.get("review_hash") != file_digest(job / "review.json"):
        raise BambooError("После утверждения изменились текст, факты, фото, правила или рецензия")
    return data
