"""Поисковая аналитика: Google Search Console, Яндекс Вебмастер, CSV и безопасные сравнения."""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import uuid
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

from .core import BambooError, config, now, safe, write_json, write_text
from .net import request, secret

FIELDS = ("date", "source", "grain", "page", "query", "impressions", "clicks", "position",
          "product_clicks", "leads", "orders", "revenue", "cost")
METRICS = FIELDS[5:]
GRAINS = {"page", "page_query", "query", "conversion"}
QUERY_REPORT_LIMIT = 100

TRANSACTIONAL = re.compile(r"\b(?:купить|заказать|цена|стоимость|магазин|в наличии|продажа)\b", re.I)
COMMERCIAL = re.compile(r"\b(?:выбрать|выбор|сравнить|сравнение|какой|какую|лучше|подарок)\b", re.I)
INFORMATIONAL = re.compile(r"\b(?:как|что|зачем|почему|чем|отлич|техника|история|уход|использовать)\b", re.I)
BRANDED = re.compile(r"\b(?:bamboo\s*pottery|бамбу\s*поттери|бамбук\s*поттери)\b", re.I)


def connect(root: Path) -> sqlite3.Connection:
    path = safe(root, "analytics/metrics.sqlite3")
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS metrics (date TEXT, source TEXT, grain TEXT, page TEXT, query TEXT, "
               "impressions REAL, clicks REAL, position REAL, product_clicks REAL, leads REAL, orders REAL, "
               "revenue REAL, cost REAL, PRIMARY KEY(date,source,grain,page,query))")
    return db


def normalize(row: dict) -> dict:
    result = {k: row.get(k) for k in FIELDS}
    date.fromisoformat(str(result["date"]))
    for key in ("source", "grain"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise BambooError(f"CSV/API: пустое поле {key}")
    if result["grain"] not in GRAINS:
        raise BambooError("grain должен быть page, page_query, query или conversion")
    result["page"] = result["page"] or ""
    result["query"] = result["query"] or ""
    if result["grain"] in ("page", "conversion") and not result["page"].strip():
        raise BambooError(f"{result['grain']}: нужна страница")
    if result["grain"] == "page_query" and (not result["page"].strip() or not result["query"].strip()):
        raise BambooError("page_query: нужны и страница, и запрос")
    if result["grain"] == "query" and not result["query"].strip():
        raise BambooError("query: нужен поисковый запрос")
    if result["grain"] not in ("page_query", "query") and result["query"]:
        raise BambooError("Запрос нельзя приписывать итоговой статистике страницы/конверсии")
    for key in METRICS:
        value = result[key]
        if value in (None, ""):
            result[key] = None
        else:
            val = float(value)
            if not math.isfinite(val) or val < 0:
                raise BambooError(f"Недопустимое значение {key}")
            result[key] = val
    return result


def ingest(root: Path, rows: list[dict]) -> dict:
    normalized = [normalize(r) for r in rows]
    keys = [tuple(r[k] for k in FIELDS[:5]) for r in normalized]
    if len(keys) != len(set(keys)):
        raise BambooError("В одной выгрузке повторяется ключ date/source/grain/page/query")
    db = connect(root)
    try:
        with db:
            placeholders = ",".join("?" for _ in FIELDS)
            update = ",".join(f"{k}=excluded.{k}" for k in METRICS)
            db.executemany(f"INSERT INTO metrics ({','.join(FIELDS)}) VALUES ({placeholders}) "
                           f"ON CONFLICT(date,source,grain,page,query) DO UPDATE SET {update}",
                           [tuple(row[k] for k in FIELDS) for row in normalized])
    finally:
        db.close()
    return {"rows_upserted": len(normalized), "note": "Повторная загрузка заменяет значения, а не складывает их"}


def import_csv(root: Path, path: Path) -> dict:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not set(FIELDS[:5]).issubset(reader.fieldnames or []):
            raise BambooError("CSV: нужны заголовки " + ",".join(FIELDS[:5]))
        rows = list(reader)
    return ingest(root, rows)


def google_token() -> str:
    if os.environ.get("BAMBOO_GSC_ACCESS_TOKEN"):
        return secret("BAMBOO_GSC_ACCESS_TOKEN")
    result = request("https://oauth2.googleapis.com/token", payload=urlencode({
        "client_id": secret("BAMBOO_GSC_CLIENT_ID"), "client_secret": secret("BAMBOO_GSC_CLIENT_SECRET"),
        "refresh_token": secret("BAMBOO_GSC_REFRESH_TOKEN"), "grant_type": "refresh_token"}).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    return result["access_token"]


def gsc_rows(property_url: str, token: str, start: str, end: str, grain: str) -> list[dict]:
    if grain not in ("page", "page_query"):
        raise BambooError("GSC: неизвестная детализация")
    dims = ["date", "page"] + (["query"] if grain == "page_query" else [])
    endpoint = "https://www.googleapis.com/webmasters/v3/sites/" + quote(property_url, safe="") + "/searchAnalytics/query"
    result = []
    for offset in range(0, 2500000, 25000):
        response = request(endpoint, payload={"startDate": start, "endDate": end, "dimensions": dims,
            "type": "web", "dataState": "final", "rowLimit": 25000, "startRow": offset},
            headers={"Authorization": "Bearer " + token}, readonly=True)
        batch = response.get("rows", [])
        for item in batch:
            keys = item["keys"]
            if len(keys) != len(dims):
                raise BambooError("GSC: неожиданная размерность rows.keys")
            result.append({"date": keys[0], "source": "google", "grain": grain, "page": keys[1],
                           "query": keys[2] if grain == "page_query" else "", "impressions": item["impressions"],
                           "clicks": item["clicks"], "position": item.get("position")})
        if len(batch) < 25000:
            return result
    raise BambooError("GSC: достигнут защитный предел пагинации; данные не импортированы")


def yandex_rows(user: str, host: str, token: str, start: str, end: str,
                text_indicator: str = "URL") -> list[dict]:
    """Query Analytics. QUERY-строки агрегированы по запросу; page — только complementary URL hint."""
    if text_indicator not in ("URL", "QUERY"):
        raise BambooError("Яндекс: text_indicator должен быть URL или QUERY")
    endpoint = ("https://api.webmaster.yandex.net/v4/user/" + quote(str(user), safe="") + "/hosts/" +
                quote(host, safe="") + "/query-analytics/list")
    result = []
    for offset in range(0, 500000, 500):
        data = request(endpoint, payload={"offset": offset, "limit": 500, "text_indicator": text_indicator,
            "device_type_indicator": "ALL", "search_location": "WEB_LOCATION"},
            headers={"Authorization": "OAuth " + token}, readonly=True)
        items = data["text_indicator_to_statistics"]
        for item in items:
            indicator = item["text_indicator"]
            if indicator["type"] != text_indicator:
                raise BambooError("Яндекс вернул неожиданную группировку")
            complementary = item.get("popular_complementary_indicator") or {}
            by_day = defaultdict(dict)
            for point in item["statistics"]:
                if start <= point["date"] <= end:
                    by_day[point["date"]][point["field"]] = point["value"]
            for day, values in by_day.items():
                if text_indicator == "URL":
                    page, query, grain = indicator["value"], "", "page"
                else:
                    query, grain = indicator["value"], "query"
                    page = complementary.get("value", "") if complementary.get("type") == "URL" else ""
                result.append({"date": day, "source": "yandex", "grain": grain, "query": query,
                    "page": page, "impressions": values.get("IMPRESSIONS"),
                    "clicks": values.get("CLICKS"), "position": values.get("POSITION")})
        if offset + len(items) >= data["count"] or not items:
            return result
    raise BambooError("Яндекс: достигнут защитный предел пагинации")


def _yandex_connection(root: Path) -> tuple[str, dict]:
    settings = config(root)["analytics"]
    user, host = settings.get("yandex_user_id"), settings.get("yandex_host_id")
    if not user or not host:
        raise BambooError("Заполните analytics.yandex_user_id и yandex_host_id")
    base = ("https://api.webmaster.yandex.net/v4/user/" + quote(str(user), safe="") +
            "/hosts/" + quote(host, safe=""))
    return base, {"Authorization": "OAuth " + secret("BAMBOO_YANDEX_TOKEN")}


def yandex_export_dates(root: Path) -> dict:
    """Доступные даты расширенной URL×query выгрузки; сеть только по явной команде."""
    base, headers = _yandex_connection(root)
    result = request(base + "/pro/serp/dates", headers=headers, readonly=True)
    dates = result.get("dates")
    if not isinstance(dates, list) or any(not isinstance(x, str) for x in dates):
        raise BambooError("Яндекс: неожиданный ответ списка доступных дат")
    for value in dates:
        date.fromisoformat(value)
    return {"dates": dates, "count": len(dates)}


def yandex_export_start(root: Path, dates: list[str], paths: list[str],
                        region_ids: list[int] | None = None, use_pro: bool = False) -> dict:
    """Создать асинхронную β-выгрузку. Операция расходует квоту и намеренно не повторяется."""
    if not dates or not paths:
        raise BambooError("Для расширенной выгрузки нужны хотя бы одна дата и один URL-путь")
    if len(paths) > 100:
        raise BambooError("Яндекс: за один запрос допускается не более 100 URL-путей")
    normalized_dates = []
    for value in dates:
        normalized_dates.append(date.fromisoformat(value).isoformat())
    normalized_paths = []
    for value in paths:
        if not isinstance(value, str) or not value.startswith("/") or "://" in value or any(ord(x) < 32 for x in value):
            raise BambooError("Яндекс: path должен быть относительным URL-путём, начинающимся с /")
        normalized_paths.append(value)
    regions = region_ids or []
    if any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in regions):
        raise BambooError("Яндекс: region-id должен быть положительным целым")
    base, headers = _yandex_connection(root)
    result = request(base + "/pro/serp/queries/download/", payload={
        "dates": normalized_dates, "paths": normalized_paths, "region_ids": regions,
        "use_pro_tariff": "true" if use_pro else "false"}, headers=headers)
    task_id = result.get("task_id")
    try:
        uuid.UUID(str(task_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BambooError("Яндекс не вернул корректный task_id") from exc
    record = {"task_id": str(task_id), "created_at": now(), "dates": normalized_dates,
              "paths": normalized_paths, "region_ids": regions, "use_pro": use_pro,
              "quota": {k: result.get(k) for k in ("free_quota_used", "pro_quota_used",
                        "total_quota_used", "free_quota_remaining", "pro_quota_remaining")}}
    write_json(safe(root, f"analytics/yandex-export-{task_id}.json"), record)
    return record


def yandex_export_status(root: Path, task_id: str) -> dict:
    """Проверить асинхронную выгрузку. Не скачивает URL и не запускает polling."""
    try:
        task = str(uuid.UUID(str(task_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise BambooError("Некорректный task_id Яндекса") from exc
    base, headers = _yandex_connection(root)
    result = request(base + "/pro/serp/queries/download/" + quote(task, safe=""), headers=headers, readonly=True)
    status = result.get("download_status")
    if status not in ("IN_PROGRESS", "SUCCESS", "FAILED"):
        raise BambooError("Яндекс: неизвестный статус расширенной выгрузки")
    output = {"task_id": task, "download_status": status}
    if status == "SUCCESS":
        output["url"] = result.get("url")
        output["note"] = "Ссылка временная. Скачайте CSV и импортируйте после проверки схемы; автоматического polling нет."
    elif status == "FAILED":
        output["error_code"] = result.get("error_code")
        output["error_message"] = result.get("error_message")
    write_json(safe(root, f"analytics/yandex-export-{task}-status.json"), {**output, "checked_at": now()})
    return output


def pull(root: Path, provider: str, start: str, end: str) -> dict:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    if a > b:
        raise BambooError("Начало периода позднее конца")
    settings = config(root)["analytics"]
    if provider == "google":
        prop = settings.get("gsc_property")
        if not prop:
            raise BambooError("Заполните analytics.gsc_property")
        token = google_token()
        rows = gsc_rows(prop, token, start, end, "page") + gsc_rows(prop, token, start, end, "page_query")
        limitation = ("GSC: page и page_query собираются отдельно; query-детализация может быть неполной "
                      "из-за лимитов/приватности API и не складывается с page.")
    elif provider == "yandex":
        user, host = settings.get("yandex_user_id"), settings.get("yandex_host_id")
        if not user or not host:
            raise BambooError("Заполните analytics.yandex_user_id и yandex_host_id")
        token = secret("BAMBOO_YANDEX_TOKEN")
        rows = (yandex_rows(user, host, token, start, end, "URL") +
                yandex_rows(user, host, token, start, end, "QUERY"))
        limitation = ("Яндекс Query Analytics: данные доступны за последние две недели. QUERY — агрегат по запросу; "
                      "поле page в grain=query содержит только popular complementary URL и не является query×page.")
    else:
        raise BambooError("Поддерживаются google и yandex")
    result = ingest(root, rows)
    write_json(safe(root, f"analytics/pull-{provider}.json"), {"provider": provider, "start": start,
               "end": end, "fetched_at": now(), "rows": len(rows), "limitation": limitation})
    return {**result, "limitation": limitation}


def summarize(rows: list[dict]) -> dict:
    out = {}
    for metric in METRICS:
        if metric == "position":
            continue
        vals = [r[metric] for r in rows if r[metric] is not None]
        out[metric] = sum(vals) if vals else None
    valid = [r for r in rows if r["position"] is not None and r["impressions"] is not None and r["impressions"] > 0]
    out["position"] = sum(r["position"] * r["impressions"] for r in valid) / sum(r["impressions"] for r in valid) if valid else None
    out["ctr"] = out["clicks"] / out["impressions"] if out["clicks"] is not None and out["impressions"] else None
    out["observed_days"] = len({r["date"] for r in rows})
    return out


def query_intent_hint(query: str) -> str:
    """Грубая эвристика для triage; не заменяет SERP-анализ."""
    if BRANDED.search(query):
        return "branded"
    if TRANSACTIONAL.search(query):
        return "transactional"
    if COMMERCIAL.search(query):
        return "commercial_research"
    if INFORMATIONAL.search(query):
        return "informational"
    return "unknown"


def _period(items: list[dict], current_start: date) -> tuple[list[dict], list[dict]]:
    boundary = current_start.isoformat()
    return ([r for r in items if r["date"] >= boundary],
            [r for r in items if r["date"] < boundary])


def _page_suggestion(cur: dict, enough: bool) -> str:
    if not enough:
        return "Недостаточно данных: наблюдать, не удалять страницу"
    position, ctr = cur["position"], cur["ctr"]
    if position is not None and position <= 10 and ctr is not None and ctr < 0.02:
        return "Гипотеза: проверить сниппет и совпадение намерения; низкий CTR оценивается только вместе с позицией и запросами"
    if position is not None and 10 < position <= 20:
        return "Гипотеза: проверить полноту ответа, внутренние ссылки и соответствие запросам до переписывания заголовка"
    return "Проверить состав запросов и связь с изделиями; автоматическое изменение не требуется"


def _query_suggestion(cur: dict, minimum: float) -> str:
    if (cur["impressions"] or 0) < minimum:
        return "Наблюдать: мало показов для решения"
    position, ctr = cur["position"], cur["ctr"]
    if position is not None and position <= 10 and ctr is not None and ctr < 0.02:
        return "Проверить заголовок/сниппет и точность ответа на этот запрос"
    if position is not None and 10 < position <= 20:
        return "Проверить покрытие вопроса, внутренние ссылки и целевую страницу"
    return "Сверить намерение запроса с назначением страницы и связанным изделием"


def _query_records(rows: list[dict], current_start: date, minimum: float) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        if row["grain"] == "page_query":
            groups[(row["source"], row["grain"], row["page"], row["query"])].append(row)
        elif row["grain"] == "query":
            groups[(row["source"], row["grain"], "", row["query"])].append(row)
    records = []
    for (source, grain, page, query), items in groups.items():
        current, previous = _period(items, current_start)
        cur, prev = summarize(current), summarize(previous)
        page_hint = page
        if grain == "query" and current:
            candidates = [r for r in current if r["page"]]
            if candidates:
                page_hint = max(candidates, key=lambda r: r["impressions"] or 0)["page"]
        records.append({"source": source, "grain": grain, "query": query,
                        "page": page if grain == "page_query" else None,
                        "page_hint": page_hint if grain == "query" else None,
                        "mapping_note": ("exact page×query dimension" if grain == "page_query"
                                         else "page_hint is Yandex popular complementary URL, not attribution"),
                        "intent_hint": query_intent_hint(query),
                        "current": cur, "previous": prev,
                        "suggestion": _query_suggestion(cur, minimum)})
    return sorted(records, key=lambda x: (x["current"]["impressions"] or 0, x["current"]["clicks"] or 0), reverse=True)


def _cannibalization_candidates(rows: list[dict], current_start: date, minimum: float) -> list[dict]:
    per_query = defaultdict(lambda: defaultdict(lambda: {"impressions": 0.0, "clicks": 0.0}))
    boundary = current_start.isoformat()
    for row in rows:
        if row["grain"] != "page_query" or row["date"] < boundary:
            continue
        metrics = per_query[(row["source"], row["query"])][row["page"]]
        metrics["impressions"] += row["impressions"] or 0
        metrics["clicks"] += row["clicks"] or 0
    result = []
    for (source, query), pages in per_query.items():
        total = sum(v["impressions"] for v in pages.values())
        visible = [{"page": p, **m} for p, m in pages.items() if m["impressions"] > 0]
        if len(visible) > 1 and total >= minimum:
            result.append({"source": source, "query": query, "total_impressions": total,
                           "pages": sorted(visible, key=lambda x: x["impressions"], reverse=True),
                           "note": "Кандидат на проверку, не доказанная каннибализация: один запрос виден у нескольких URL."})
    return sorted(result, key=lambda x: x["total_impressions"], reverse=True)


def _conversion_records(rows: list[dict], current_start: date) -> list[dict]:
    groups = defaultdict(list)
    for row in rows:
        if row["grain"] == "conversion":
            groups[(row["source"], row["page"])].append(row)
    result = []
    for (source, page), items in sorted(groups.items()):
        current, previous = _period(items, current_start)
        cur, prev = summarize(current), summarize(previous)
        leads, orders = cur["leads"], cur["orders"]
        cur["lead_to_order_rate"] = (orders / leads if orders is not None and leads else None)
        result.append({"source": source, "page": page, "current": cur, "previous": prev,
                       "note": "Конверсии показываются отдельно и не приписываются поисковой системе без явной атрибуции источника."})
    return result


def report(root: Path, end: str, days: int = 7) -> dict:
    if not 1 <= days <= 366:
        raise BambooError("Длина периода: 1–366 дней")
    last = date.fromisoformat(end)
    current_start = last - timedelta(days=days - 1)
    previous_start = current_start - timedelta(days=days)
    db = connect(root)
    try:
        rows = [dict(r) for r in db.execute("SELECT * FROM metrics WHERE date BETWEEN ? AND ?",
                                           (previous_start.isoformat(), end))]
    finally:
        db.close()

    minimum = config(root)["analytics"].get("min_impressions", 100)
    page_groups = defaultdict(list)
    for row in rows:
        if row["grain"] == "page":
            page_groups[(row["source"], row["page"])].append(row)

    pages = []
    for (source, page), items in sorted(page_groups.items()):
        current, previous = _period(items, current_start)
        cur, prev = summarize(current), summarize(previous)
        enough = cur["observed_days"] >= max(1, days // 2) and (cur["impressions"] or 0) >= minimum
        old, new = prev["clicks"], cur["clicks"]
        comparable = cur["observed_days"] == days and prev["observed_days"] == days
        change = (new - old) / old if comparable and old and new is not None else None
        pages.append({"source": source, "grain": "page", "page": page, "current": cur, "previous": prev,
                      "click_change_fraction": change, "suggestion": _page_suggestion(cur, enough)})

    all_queries = _query_records(rows, current_start, minimum)
    queries = all_queries[:QUERY_REPORT_LIMIT]
    candidates = _cannibalization_candidates(rows, current_start, minimum)
    conversions = _conversion_records(rows, current_start)

    output = {
        "current": [current_start.isoformat(), end],
        "previous": [previous_start.isoformat(), (current_start - timedelta(days=1)).isoformat()],
        "pages": pages,
        "queries": queries,
        "query_count_total": len(all_queries),
        "query_count_returned": len(queries),
        "cannibalization_candidates": candidates,
        "conversions": conversions,
        "policy": ("Page, page_query/query и conversion не складываются между собой. Intent — эвристическая подсказка. "
                   "Яндекс grain=query не является парой query×page. Рекомендации — гипотезы, не автоматические правки.")
    }
    write_json(safe(root, "analytics/report.json"), output)

    lines = ["# Отчёт Bamboo Pottery", f"\nПериод: {current_start} — {end}", "\n" + output["policy"]]
    for item in pages:
        lines += [f'\n## {item["page"]} ({item["source"]})',
                  f'Клики: {item["current"]["clicks"]}; показы: {item["current"]["impressions"]}; '
                  f'позиция: {item["current"]["position"]}; дней с данными: {item["current"]["observed_days"]}/{days}.',
                  item["suggestion"]]
    if queries:
        lines.append("\n# Поисковые запросы")
        for item in queries[:20]:
            target = item["page"] or item["page_hint"] or "без URL-привязки"
            lines.append(f'- {item["query"]} [{item["source"]}, {item["intent_hint"]}] → {target}; '
                         f'показы {item["current"]["impressions"]}, клики {item["current"]["clicks"]}, '
                         f'позиция {item["current"]["position"]}. {item["suggestion"]}')
    if candidates:
        lines.append("\n# Кандидаты на каннибализацию")
        for item in candidates[:20]:
            lines.append(f'- {item["query"]}: ' + "; ".join(p["page"] for p in item["pages"]))
    if conversions:
        lines.append("\n# Конверсии")
        for item in conversions:
            cur = item["current"]
            lines.append(f'- {item["page"]} ({item["source"]}): переходы к товару {cur["product_clicks"]}, '
                         f'лиды {cur["leads"]}, заказы {cur["orders"]}, выручка {cur["revenue"]}, затраты {cur["cost"]}.')
    if not pages and not queries and not conversions:
        lines.append("\nДанных нет. Это не означает отсутствие трафика.")
    write_text(safe(root, "analytics/report.md"), "\n".join(lines) + "\n")
    return output
