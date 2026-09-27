"""Локальная готовность и не более трёх предложений. Без сети, LLM и автоправок."""
from __future__ import annotations

import os
import sqlite3
from datetime import date
from pathlib import Path

from .core import BambooError, config, read_json, safe
from .quality import http_url, require_approval, validate


def workspace_readiness(root: Path) -> dict:
    """Диагностика ничего не создаёт и не принимает наличие токена за авторизацию."""
    result = {"initialized": False, "issues": [], "next_steps": [],
              "products": {"total": 0, "confirmed": 0}, "voice_samples": 0, "jobs": [],
              "analytics": {"status": "no_data", "sources": []}, "providers": {}}
    steps = result["next_steps"]
    if not safe(root, "bamboo.json").exists():
        steps.append("Выполните python bamboo.py init в рабочей папке.")
        return result
    try:
        cfg = config(root)
    except (BambooError, ValueError, TypeError):
        result["issues"].append("Исправьте bamboo.json: конфигурация не прошла проверку.")
        steps.append("Проверьте структуру bamboo.json по docs/START.md; существующий файл не перезаписывается.")
        return result
    result["initialized"] = True
    for path in sorted(safe(root, "content/products").glob("*.json")):
        result["products"]["total"] += 1
        try:
            obj = read_json(safe(root, path.relative_to(root).as_posix()))
            if not isinstance(obj, dict):
                raise ValueError()
            if obj.get("id") != path.stem:
                raise ValueError()
            if obj.get("confirmed") is True:
                result["products"]["confirmed"] += 1
        except (BambooError, ValueError, TypeError):
            result["issues"].append(f"Некорректный паспорт: content/products/{path.name}")
    for path in sorted(safe(root, "content/voice").glob("*.md")):
        if path.name.casefold() == "readme.md":
            continue
        try:
            path = safe(root, path.relative_to(root).as_posix())
            if path.read_text(encoding="utf-8").strip():
                result["voice_samples"] += 1
        except (BambooError, OSError, UnicodeError):
            result["issues"].append(f"Не читается образец голоса: {path.name}")
    for folder in sorted(safe(root, "content/jobs").glob("*")):
        if not folder.is_dir():
            continue
        report = validate(root, folder.name)
        state = "needs_facts_or_fixes"
        if report["ok"]:
            state = "ready_for_human_review"
            try:
                require_approval(root, folder.name)
                state = "approved"
            except (BambooError, OSError):
                pass
        result["jobs"].append({"slug": folder.name, "state": state,
                               "errors": len(report["errors"]), "warnings": len(report["warnings"])})
    settings = cfg["analytics"]
    groups = {
        "google": (["gsc_property"], ["BAMBOO_GSC_ACCESS_TOKEN"]),
        "yandex": (["yandex_user_id", "yandex_host_id"], ["BAMBOO_YANDEX_TOKEN"]),
    }
    for provider, (fields, env) in groups.items():
        missing = ["analytics." + k for k in fields if not settings.get(k)]
        if provider == "google" and not os.environ.get(env[0], "").strip():
            env = ["BAMBOO_GSC_CLIENT_ID", "BAMBOO_GSC_CLIENT_SECRET", "BAMBOO_GSC_REFRESH_TOKEN"]
        missing += [k for k in env if not os.environ.get(k, "").strip()]
        result["providers"][provider] = {"configured": not missing, "missing": missing,
                                           "authorization": "not_tested"}
    database = safe(root, "analytics/metrics.sqlite3")
    if database.exists():
        db = None
        try:
            db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            rows = db.execute("SELECT source, MIN(date), MAX(date), COUNT(*) FROM metrics GROUP BY source ORDER BY source").fetchall()
            result["analytics"] = {"status": "available" if rows else "no_data",
                "sources": [{"source": s, "first_date": first, "last_date": last, "rows": n} for s, first, last, n in rows]}
        except sqlite3.Error:
            result["analytics"]["status"] = "unreadable"
            result["issues"].append("Не читается analytics/metrics.sqlite3; сохраните копию и восстановите из исходной выгрузки.")
        finally:
            if db is not None:
                db.close()
    if not result["products"]["confirmed"]:
        steps.append("Для первого товара заполните один паспорт по examples/product.template.json; общий пост возможен и без товара.")
    if not result["voice_samples"]:
        steps.append("Добавьте разрешённые тексты мастера в content/voice/; до этого используйте нейтральный голос без выдуманных историй.")
    broken = next((j for j in result["jobs"] if j["state"] == "needs_facts_or_fixes"), None)
    review = next((j for j in result["jobs"] if j["state"] == "ready_for_human_review"), None)
    if broken:
        steps.insert(0, f"Проверьте задание: python bamboo.py validate {broken['slug']}")
    elif review:
        steps.insert(0, f"Посмотрите все форматы превью: python bamboo.py export {review['slug']}; утверждает человек.")
    elif not result["jobs"]:
        steps.append('Создайте один пост: python bamboo.py new first-post --topic "Ваша тема" --formats vk')
    if result["analytics"]["status"] == "available":
        steps.append("Выполните python bamboo.py analytics-report: дата берётся из сохранённых данных, подключения не нужны.")
    else:
        steps.append("Для аналитики импортируйте реальный CSV или настройте один источник. Google и Яндекс не обязательны одновременно.")
    result["note"] = "Паспорта и образцы — локальные декларации, не независимая проверка; сетевой авторизации не было."
    return result


def readiness_text(result: dict) -> str:
    data = result["readiness"]
    lines = [f"Bamboo {result['version']} — проверка рабочей папки", f"Папка: {result['workspace']}",
             f"Конфигурация: {'прочитана' if data['initialized'] else 'нужна настройка'}",
             f"Паспорта: {data['products']['confirmed']} подтверждено из {data['products']['total']}; образцы голоса: {data['voice_samples']}"]
    for job in data["jobs"]:
        label = {"approved": "утверждено", "ready_for_human_review": "ждёт проверки человеком",
                 "needs_facts_or_fixes": "нужны факты или исправления"}[job["state"]]
        lines.append(f"Задание {job['slug']}: {label}; ошибок {job['errors']}, предупреждений {job['warnings']}")
    for item in data["analytics"]["sources"]:
        lines.append(f"Данные {item['source']}: {item['first_date']} — {item['last_date']}, строк {item['rows']}")
    for provider, item in data["providers"].items():
        lines.append(f"{provider}: " + ("параметры заданы; доступ не проверялся" if item["configured"] else "не настроен (не мешает локальной работе)"))
    lines += ["", *data["issues"], "Следующий шаг:", *[f"{i}. {s}" for i, s in enumerate(data["next_steps"], 1)], "", result["note"]]
    return "\n".join(lines)


def content_opportunities(root: Path, report: dict, queries: list[dict], minimum: float) -> dict:
    """Выводится только рекомендация с доказательством. Нет генерации страниц/товаров/спроса."""
    actions = []
    used = set()
    start, end = report["current"]
    requested_days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1

    def usable(item: dict) -> bool:
        current = item["current"]
        coverage = current.get("metric_coverage", {})
        return (current.get("observed_days", 0) == requested_days
                and coverage.get("impressions", {}).get("complete") is True
                and (current.get("impressions") or 0) >= minimum)

    def add(kind: str, item: dict, action: str, reason: str, product_ids: list[str] | None = None) -> None:
        target = item.get("page") or ("query:" + item["query"])
        if len(actions) >= 3 or target in used:
            return
        used.add(target)
        actions.append({"kind": kind, "action": action, "reason": reason,
            "target_url": item.get("page"), "product_ids": product_ids or [],
            "evidence": {"source": item["source"], "grain": item["grain"], "query": item["query"],
                         "period": [start, end], "impressions": item["current"]["impressions"],
                         "clicks": item["current"]["clicks"], "position": item["current"]["position"]},
            "success_check": "После согласованной правки сравнить сопоставимые периоды и подтверждённые обращения, не только показы.",
            "stop_condition": "Не менять страницу, если выдача/фактура не подтверждает гипотезу или измерения неполны.",
            "human_decision_required": True})

    eligible = [q for q in queries if usable(q)]
    exact = {(q["source"], q["query"], q["page"]): q for q in eligible if q.get("page")}
    for candidate in report["intent_mismatch_candidates"]:
        q = exact.get((candidate["source"], candidate["query"], candidate["page"]))
        if q:
            add("review_existing_page", q, "Проверить соответствие существующей страницы запросу и путь к товару.", candidate["reason"])
    for candidate in report["cannibalization_candidates"]:
        q = next((exact.get((candidate["source"], candidate["query"], p["page"])) for p in candidate["pages"]
                  if exact.get((candidate["source"], candidate["query"], p["page"]))), None)
        if q:
            add("review_overlap", q, "Сопоставить назначение URL; не удалять и не объединять их автоматически.", "Один запрос наблюдается у нескольких URL в одном источнике.")
    for folder in sorted(safe(root, "content/jobs").glob("*")):
        if not folder.is_dir() or folder.is_symlink():
            continue
        brief = read_json(safe(root, (folder / "brief.json").relative_to(root).as_posix()))
        url = (brief.get("seo") or {}).get("target_url")
        q = next((q for q in eligible if q.get("page") == url and url), None)
        if not q or not brief.get("product_ids"):
            continue
        pack_path = safe(root, (folder / "pack.json").relative_to(root).as_posix())
        pack = read_json(pack_path)
        text = "\n".join(str(f.get("text", "")) + " " + str(f.get("cta", "")) for f in pack.get("formats", {}).values())
        products = []
        for pid in brief["product_ids"]:
            product = read_json(safe(root, f"content/products/{pid}.json"))
            target = product.get("product_url")
            if product.get("confirmed") is not True or not target:
                continue
            http_url(target)
            if target not in text:
                products.append(pid)
        if products:
            add("review_product_link", q, "Проверить и добавить уместный переход к связанному подтверждённому товару.", "В тексте пакета не найден URL связанного товара; ссылки шаблона сайта отдельно не проверялись.", products)
    if not actions:
        if not queries:
            state, message = "no_data", "Нет сохранённых поисковых запросов. Импортируйте данные; новые темы из отсутствующей статистики не создаются."
        elif not eligible:
            state, message = "insufficient_data", "Не хватает наблюдений за всеми днями выбранного периода или показов. Порог — эвристика, не статистическая значимость."
        else:
            state, message = "no_action", "Данные есть, но подтверждённого повода менять страницы не найдено. Продолжайте наблюдение; автоматических новых тем нет."
    else:
        state, message = "review", "Сначала улучшить существующие материалы. Предложения не являются утверждением причинности или обещанием роста."
    return {"status": state, "message": message, "actions": actions}
