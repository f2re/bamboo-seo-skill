"""Поисковая аналитика: API, CSV, SQLite upsert и сравнение равных периодов."""
from __future__ import annotations

import csv
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

from .core import BambooError, config, now, safe, write_json, write_text
from .net import request, secret

FIELDS = ("date", "source", "grain", "page", "query", "impressions", "clicks", "position",
          "product_clicks", "leads", "orders", "revenue", "cost")
METRICS = FIELDS[5:]


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
    for key in ("source", "grain", "page"):
        if not isinstance(result[key], str) or not result[key].strip():
            raise BambooError(f"CSV/API: пустое поле {key}")
    if result["grain"] not in ("page", "page_query", "conversion"):
        raise BambooError("grain должен быть page, page_query или conversion")
    result["query"] = result["query"] or ""
    if result["grain"] != "page_query" and result["query"]:
        raise BambooError("Запрос нельзя приписывать итоговой статистике страницы")
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
    normalized = [normalize(r) for r in rows]  # Все строки проверить до первой записи.
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
    # OAuth refresh: стандартное form-urlencoded тело, секреты только из окружения.
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


def yandex_rows(user: str, host: str, token: str, start: str, end: str) -> list[dict]:
    endpoint = ("https://api.webmaster.yandex.net/v4/user/" + quote(str(user), safe="") + "/hosts/" +
                quote(host, safe="") + "/query-analytics/list")
    result = []
    for offset in range(0, 500000, 500):
        data = request(endpoint, payload={"offset": offset, "limit": 500, "text_indicator": "URL",
            "device_type_indicator": "ALL", "search_location": "WEB_LOCATION"},
            headers={"Authorization": "OAuth " + token}, readonly=True)
        items = data["text_indicator_to_statistics"]
        for item in items:
            if item["text_indicator"]["type"] != "URL":
                raise BambooError("Яндекс вернул не URL-группировку")
            by_day = defaultdict(dict)
            for point in item["statistics"]:
                if start <= point["date"] <= end:
                    by_day[point["date"]][point["field"]] = point["value"]
            for day, values in by_day.items():
                # popular_complementary_indicator — только популярный запрос, НЕ разрез всех кликов.
                result.append({"date": day, "source": "yandex", "grain": "page", "query": "",
                    "page": item["text_indicator"]["value"], "impressions": values.get("IMPRESSIONS"),
                    "clicks": values.get("CLICKS"), "position": values.get("POSITION")})
        if offset + len(items) >= data["count"] or not items:
            return result
    raise BambooError("Яндекс: достигнут защитный предел пагинации")


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
        limitation = "GSC: собственная временная зона PT, final; API не гарантирует все строки запросов"
    elif provider == "yandex":
        user, host = settings.get("yandex_user_id"), settings.get("yandex_host_id")
        if not user or not host:
            raise BambooError("Заполните analytics.yandex_user_id и yandex_host_id")
        rows = yandex_rows(user, host, secret("BAMBOO_YANDEX_TOKEN"), start, end)
        limitation = "Яндекс: API отдаёт последние две недели; загружаются только доступные даты, не нули за отсутствующие дни"
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


def report(root: Path, end: str, days: int = 7) -> dict:
    if not 1 <= days <= 366:
        raise BambooError("Длина периода: 1–366 дней")
    last = date.fromisoformat(end)
    current_start = last - timedelta(days=days - 1)
    previous_start = current_start - timedelta(days=days)
    db = connect(root)
    try:
        rows = [dict(r) for r in db.execute("SELECT * FROM metrics WHERE date BETWEEN ? AND ? AND grain != 'page_query'",
                                           (previous_start.isoformat(), end))]
    finally:
        db.close()
    groups = defaultdict(list)
    for row in rows:
        groups[(row["source"], row["grain"], row["page"])].append(row)
    results = []
    minimum = config(root)["analytics"].get("min_impressions", 100)
    for (source, grain, page), items in sorted(groups.items()):
        cur = summarize([r for r in items if r["date"] >= current_start.isoformat()])
        prev = summarize([r for r in items if r["date"] < current_start.isoformat()])
        enough = cur["observed_days"] >= max(1, days // 2) and (cur["impressions"] or 0) >= minimum
        suggestion = "Недостаточно данных: наблюдать, не удалять страницу"
        if enough:
            suggestion = "Проверить релевантность запросов и связь с изделиями; изменение не требуется автоматически"
            if cur["ctr"] is not None and cur["ctr"] < 0.02:
                suggestion = "Гипотеза: проверить заголовок/сниппет с учётом позиции, устройства и брендового спроса"
        old, new = prev["clicks"], cur["clicks"]
        comparable = cur["observed_days"] == days and prev["observed_days"] == days
        change = (new - old) / old if comparable and old and new is not None else None
        results.append({"source": source, "grain": grain, "page": page, "current": cur, "previous": prev,
                        "click_change_fraction": change, "suggestion": suggestion})
    output = {"current": [current_start.isoformat(), end],
              "previous": [previous_start.isoformat(), (current_start - timedelta(days=1)).isoformat()],
              "pages": results, "policy": "Рекомендации — гипотезы. Нет автоматического удаления/изменения стратегии. Источники и детализации не складываются."}
    write_json(safe(root, "analytics/report.json"), output)
    lines = ["# Отчёт Bamboo Pottery", f"\nПериод: {current_start} — {end}", "\n" + output["policy"]]
    for item in results:
        lines += [f'\n## {item["page"]} ({item["source"]}, {item["grain"]})',
                  f'Клики: {item["current"]["clicks"]}; показы: {item["current"]["impressions"]}; дней с данными: {item["current"]["observed_days"]}/{days}.',
                  item["suggestion"]]
    if not results:
        lines.append("\nДанных нет. Это не означает отсутствие трафика.")
    write_text(safe(root, "analytics/report.md"), "\n".join(lines) + "\n")
    return output
