"""Коммерческий граф: товары, коллекции, контент и безопасные предложения перелинковки."""
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

from .core import BambooError, config, read_json, safe, slug, write_json
from .quality import http_url

PAGE_TYPES = {"product", "category", "article", "technique", "term", "social"}


def _json_files(path: Path) -> list[Path]:
    return sorted(p for p in path.glob("*.json") if p.is_file() and not p.is_symlink())


def page_index(root: Path) -> dict[str, dict]:
    result = {}
    jobs = safe(root, "content/jobs")
    if not jobs.exists():
        return result
    for folder in sorted(p for p in jobs.iterdir() if p.is_dir() and not p.is_symlink()):
        brief_path = folder / "brief.json"
        if not brief_path.is_file():
            continue
        brief = read_json(brief_path)
        seo = brief.get("seo") or {}
        url = seo.get("target_url")
        if url:
            try:
                http_url(url)
            except BambooError:
                continue
            result[url] = {"slug": brief.get("slug", folder.name), "page_type": seo.get("page_type"),
                           "cluster": seo.get("cluster"), "product_ids": brief.get("product_ids", [])}
    return result


def build_graph(root: Path) -> dict:
    products, collections, jobs = {}, {}, {}
    warnings, edges, suggestions, structured = [], [], [], []
    site_url = config(root).get("site_url")
    site_host = urlsplit(site_url).hostname if site_url else None

    product_dir = safe(root, "content/products")
    for path in _json_files(product_dir):
        obj = read_json(path)
        pid = obj.get("id")
        if not isinstance(pid, str):
            warnings.append({"code": "product_id", "path": str(path.relative_to(root)), "message": "Нет строкового id"})
            continue
        slug(pid)
        products[pid] = obj
        if obj.get("confirmed") is True and not obj.get("product_url"):
            warnings.append({"code": "product_url_missing", "product_id": pid,
                             "message": "Подтверждённый товар не имеет product_url; SEO/CTA-связь ограничена"})
        if obj.get("product_url"):
            http_url(obj["product_url"])
            product_host = urlsplit(obj["product_url"]).hostname
            required = {
                "name": obj.get("name"),
                "image": obj.get("photo_set"),
                "offers.price": obj.get("price"),
                "offers.priceCurrency": obj.get("currency"),
                "offers.availability": obj.get("availability")
            }
            missing = [key for key, value in required.items() if value in (None, "", [])]
            structured.append({
                "product_id": pid,
                "product_url": obj["product_url"],
                "same_site_purchase_page": bool(site_host and product_host == site_host),
                "merchant_listing_candidate": bool(site_host and product_host == site_host and not missing),
                "missing_required_fields": missing,
                "note": ("Product/Offer разметку допустимо генерировать только на собственной странице покупки; "
                         "внешняя карточка ВК/магазина не размечается на статье Bamboo.")
            })

    collection_dir = safe(root, "content/collections")
    if collection_dir.exists():
        for path in _json_files(collection_dir):
            obj = read_json(path)
            cid = obj.get("id")
            if not isinstance(cid, str):
                warnings.append({"code": "collection_id", "path": str(path.relative_to(root)), "message": "Нет строкового id"})
                continue
            slug(cid)
            collections[cid] = obj
            if obj.get("url"):
                http_url(obj["url"])
            for pid in obj.get("product_ids", []):
                if pid not in products:
                    warnings.append({"code": "collection_missing_product", "collection_id": cid, "product_id": pid,
                                     "message": "Коллекция ссылается на отсутствующий товар"})
                else:
                    edges.append({"from": f"collection:{cid}", "to": f"product:{pid}", "relation": "contains"})

    job_dir = safe(root, "content/jobs")
    target_urls = defaultdict(list)
    if job_dir.exists():
        for folder in sorted(p for p in job_dir.iterdir() if p.is_dir() and not p.is_symlink()):
            brief_path = folder / "brief.json"
            if not brief_path.is_file():
                continue
            brief = read_json(brief_path)
            name = brief.get("slug", folder.name)
            seo = brief.get("seo") or {}
            page_type, cluster, target = seo.get("page_type"), seo.get("cluster"), seo.get("target_url")
            jobs[name] = {"page_type": page_type, "cluster": cluster, "target_url": target,
                          "product_ids": brief.get("product_ids", []), "related_urls": seo.get("related_urls", [])}
            if page_type and page_type not in PAGE_TYPES:
                warnings.append({"code": "page_type", "job": name, "message": "Неизвестный page_type"})
            if page_type and page_type != "social" and not cluster:
                warnings.append({"code": "cluster_missing", "job": name,
                                 "message": "Индексируемая задача не имеет SEO-кластера"})
            if target:
                http_url(target)
                target_urls[target].append(name)
            for url in seo.get("related_urls", []):
                http_url(url)
                edges.append({"from": f"job:{name}", "to": url, "relation": "related_url"})
            for pid in brief.get("product_ids", []):
                if pid not in products:
                    warnings.append({"code": "job_missing_product", "job": name, "product_id": pid,
                                     "message": "Задача ссылается на отсутствующий товар"})
                    continue
                edges.append({"from": f"job:{name}", "to": f"product:{pid}", "relation": "supports"})
                product_url = products[pid].get("product_url")
                if target and product_url and product_url not in seo.get("related_urls", []):
                    suggestions.append({"job": name, "from_url": target, "to_url": product_url,
                                        "relation": "product",
                                        "reason": "Материал связан с product_id, но URL товара не указан среди related_urls"})

    for url, names in target_urls.items():
        if len(names) > 1:
            warnings.append({"code": "duplicate_target_url", "url": url, "jobs": names,
                             "message": "Несколько задач заявляют один target_url"})

    clusters = defaultdict(lambda: {"jobs": [], "products": [], "collections": []})
    for name, item in jobs.items():
        if item.get("cluster"):
            clusters[item["cluster"]]["jobs"].append(name)
    for pid, item in products.items():
        if item.get("collection"):
            clusters[item["collection"]]["products"].append(pid)
            if item["collection"] not in collections:
                warnings.append({"code": "missing_collection", "product_id": pid, "collection": item["collection"],
                                 "message": "Товар указывает коллекцию, для которой нет паспорта"})
    for cid, item in collections.items():
        clusters[item.get("cluster") or cid]["collections"].append(cid)

    result = {"schema_version": 1,
              "nodes": {"products": sorted(products), "collections": sorted(collections), "jobs": sorted(jobs)},
              "jobs": jobs, "edges": edges, "clusters": dict(clusters),
              "internal_link_suggestions": suggestions,
              "structured_data_candidates": structured, "warnings": warnings}
    write_json(safe(root, "content/commerce-graph.json"), result)
    return result
