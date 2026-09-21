"""Экспорт, локальный SEO-сайт и реальные черновики/публикации WordPress."""
from __future__ import annotations

import base64
import html
import json
import mimetypes
import re
import shutil
from pathlib import Path
from urllib.parse import urlencode

from .core import (BambooError, approval_token, config, file_digest, job_path, now,
                   read_json, safe, snapshot, write_json, write_text)
from .net import request, secret
from .quality import clean, http_url, require_approval, validate


def inline(text: str) -> str:
    """Малое явное подмножество Markdown; произвольный HTML не исполняется."""
    def link(match):
        try:
            url = http_url(match.group(2))
        except BambooError:
            return html.escape(match.group(1))
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(match.group(1))}</a>'
    parts = re.split(r"(\[[^\]\n]+\]\(https?://[^\s)]+\))", text)
    result = []
    for part in parts:
        match = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", part)
        result.append(link(match) if match else html.escape(part))
    return "".join(result)


def markdown(text: str) -> str:
    result = []
    for block in re.split(r"\n\s*\n", clean(text)):
        if not block.strip():
            continue
        if re.match(r"^#{1,6} ", block):
            head, _, rest = block.partition("\n")
            level = min(6, max(2, len(head) - len(head.lstrip("#"))))
            result.append(f"<h{level}>{inline(head.lstrip('#').strip())}</h{level}>")
            if rest:
                result.append("<p>" + inline(rest).replace("\n", "<br>") + "</p>")
        elif all(line.startswith("- ") for line in block.splitlines()):
            result.append("<ul>" + "".join("<li>" + inline(line[2:]) + "</li>" for line in block.splitlines()) + "</ul>")
        else:
            result.append("<p>" + inline(block).replace("\n", "<br>") + "</p>")
    return "\n".join(result)


def body(root: Path, name: str, photo_urls: list[str] | None = None) -> str:
    job = job_path(root, name)
    pack = read_json(job / "pack.json")
    if "article" not in pack["formats"]:
        raise BambooError("Для публикации на сайте требуется формат article")
    article = pack["formats"]["article"]
    text = markdown(article["text"]) + markdown(article["cta"])
    for photo, url in zip(pack["photos"], photo_urls or []):
        text += (f'<figure><img loading="lazy" src="{html.escape(url, quote=True)}" '
                 f'alt="{html.escape(photo["alt"], quote=True)}">'
                 f'<figcaption>{html.escape(photo.get("caption", ""))}</figcaption></figure>')
    used = set(article.get("claims", []))
    source_ids = {sid for c in read_json(job / "claims.json") if c["id"] in used for sid in c["source_ids"]}
    sources = [s for s in read_json(job / "sources.json") if s["id"] in source_ids]
    if sources:
        text += "<h2>Источники и фактура</h2><ul>"
        for source in sources:
            title = html.escape(source["title"])
            if source.get("url"):
                title = f'<a href="{html.escape(http_url(source["url"]), quote=True)}">{title}</a>'
            text += "<li>" + title + "</li>"
        text += "</ul>"
    return text


def page(title: str, description: str, content: str, canonical: str | None = None,
         draft: bool = True) -> str:
    esc = html.escape
    head = '<meta name="robots" content="noindex,nofollow">' if draft else ""
    if canonical:
        head += f'<link rel="canonical" href="{esc(canonical, quote=True)}">'
    return (f'<!doctype html>\n<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f'<title>{esc(title)}</title><meta name="description" content="{esc(description, quote=True)}">{head}'
            '<style>body{font:18px/1.65 system-ui,sans-serif;max-width:800px;margin:3rem auto;padding:0 1rem}'
            'img{max-width:100%;height:auto}figcaption{font-size:.85em}nav{margin-bottom:2rem}</style>'
            f'<body><nav>Bamboo Pottery{" · ЧЕРНОВИК" if draft else ""}</nav><main><h1>{esc(title)}</h1>{content}</main></body></html>')


def export(root: Path, name: str) -> dict:
    report = validate(root, name)
    if not report["ok"]:
        raise BambooError("Сначала устраните ошибки validate; экспорт не выполнен")
    pack = read_json(job_path(root, name) / "pack.json")
    dest = safe(root, f"exports/{name}")
    dest.mkdir(parents=True, exist_ok=True)
    urls = []
    for photo in pack["photos"]:
        source = safe(root, photo["path"])
        rel = "media/" + photo["sha256"] + source.suffix.lower()
        target = safe(dest, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        urls.append(rel)
    for fmt, item in pack["formats"].items():
        write_text(safe(dest, f"{fmt}.txt"), clean(item["text"]) + "\n\n" + clean(item["cta"]) + "\n")
    write_json(dest / "photos.json", [{"file": url, "alt": p["alt"], "caption": p.get("caption", "")} for p, url in zip(pack["photos"], urls)])
    content = (body(root, name, urls) if "article" in pack["formats"] else
               "".join(f"<h2>{fmt}</h2>" + markdown(v["text"]) + markdown(v["cta"]) for fmt, v in pack["formats"].items()))
    write_text(dest / "preview.html", page(pack["title"], pack["description"], content))
    write_json(dest / "manifest.json", {"slug": name, "content_hash": snapshot(root, name), "draft": True, "created_at": now()})
    return {"path": str(dest), "preview": str(dest / "preview.html"), "published": False}


def publish_static(root: Path, name: str) -> dict:
    require_approval(root, name)
    base = http_url(config(root).get("site_url"), https_only=True).rstrip("/")
    exported = export(root, name)
    src = Path(exported["path"])
    dest = safe(root, f"site/journal/{name}")
    dest.mkdir(parents=True, exist_ok=True)
    pack = read_json(job_path(root, name) / "pack.json")
    photos = read_json(src / "photos.json")
    for photo in photos:
        target = safe(dest, photo["file"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(safe(src, photo["file"]), target)
    canonical = base + f"/journal/{name}/"
    write_text(dest / "index.html", page(pack["title"], pack["description"],
                                       body(root, name, [p["file"] for p in photos]), canonical, draft=False))
    registry_path = safe(root, "site/registry.json")
    registry = read_json(registry_path) if registry_path.exists() else {}
    registry[name] = {"title": pack["title"], "url": canonical, "updated_at": now(), "content_hash": snapshot(root, name)}
    write_json(registry_path, registry)
    links = "<ul>" + "".join(f'<li><a href="journal/{html.escape(k)}/">{html.escape(v["title"])}</a></li>' for k, v in sorted(registry.items())) + "</ul>"
    write_text(safe(root, "site/index.html"), page("Журнал Bamboo Pottery", "Чайная керамика и работа мастера", links, base + "/", False))
    entries = "".join(f'<url><loc>{html.escape(v["url"])}</loc><lastmod>{v["updated_at"]}</lastmod></url>' for v in registry.values())
    write_text(safe(root, "site/sitemap.xml"), '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + entries + '</urlset>')
    write_text(safe(root, "site/robots.txt"), f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n")
    return {"status": "built_locally", "path": str(dest), "url_after_upload": canonical,
            "note": "Каталог site нужно перенести на свой хостинг; удалённой загрузки не было"}


def wp_setup(root: Path) -> tuple[str, dict]:
    base = http_url(config(root).get("wordpress_url"), https_only=True).rstrip("/")
    auth = base64.b64encode((secret("BAMBOO_WP_USER") + ":" + secret("BAMBOO_WP_APP_PASSWORD")).encode()).decode()
    return base + "/wp-json/wp/v2", {"Authorization": "Basic " + auth}


def publish_wordpress(root: Path, name: str, *, live: bool = False) -> dict:
    require_approval(root, name)
    base, headers = wp_setup(root)
    job = job_path(root, name)
    ledger_path = job / "wordpress.json"
    ledger = read_json(ledger_path) if ledger_path.exists() else {"media": {}}
    if ledger.get("base") not in (None, base):
        raise BambooError("WordPress-адрес изменился; не использовать ID из другого сайта")
    if ledger.get("pending"):
        raise BambooError("Прежний запрос не завершён; выполните wp-reconcile, не создавайте дубликат")
    sha = snapshot(root, name)
    status = "publish" if live else "draft"
    if ledger.get("status") == "publish" and not live:
        raise BambooError("Запись уже публична; обновляйте с --live либо управляйте снятием публикации вручную")
    if ledger.get("content_hash") == sha and ledger.get("status") == status:
        return {**ledger, "unchanged": True}
    if not ledger.get("id"):
        existing = request(base + "/posts?" + urlencode({"slug": name, "context": "edit", "status": "any"}), headers=headers, readonly=True)
        if existing:
            raise BambooError("Такой slug уже есть в WordPress; чужую запись не перезаписываем")
    if ledger.get("id"):
        current = request(base + f'/posts/{int(ledger["id"])}?context=edit', headers=headers, readonly=True)
        marker = f'<!-- bamboo:{name}:{ledger.get("content_hash", "")} -->'
        if (marker not in current.get("content", {}).get("raw", "") or
                (ledger.get("modified_gmt") and current.get("modified_gmt") != ledger["modified_gmt"])):
            raise BambooError("Запись изменена вне Bamboo; сначала согласуйте изменения вручную")
    pack = read_json(job / "pack.json")
    urls = []
    for photo in pack["photos"]:
        key = photo["sha256"]
        if key not in ledger["media"]:
            source = safe(root, photo["path"])
            ledger.update({"base": base, "pending": True, "pending_kind": "media", "pending_media": key})
            write_json(ledger_path, ledger)
            media = request(base + "/media", payload=source.read_bytes(), headers={**headers,
                "Content-Type": mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                "Content-Disposition": f'attachment; filename="{key}{source.suffix.lower()}"'})
            ledger["media"][key] = {"id": media["id"], "url": http_url(media["source_url"], True)}
            ledger.update({"base": base, "pending": False})
            write_json(ledger_path, ledger)
        request(base + f'/media/{int(ledger["media"][key]["id"])}',
                payload={"alt_text": photo["alt"], "caption": photo.get("caption", "")}, headers=headers)
        urls.append(ledger["media"][key]["url"])
    marker = f"<!-- bamboo:{name}:{sha} -->"
    payload = {"slug": name, "title": pack["title"], "excerpt": pack["description"],
               "content": body(root, name, urls) + marker, "status": status}
    ledger.update({"pending": True, "pending_kind": "post", "pending_hash": sha, "base": base})
    write_json(ledger_path, ledger)
    endpoint = base + "/posts" + (f'/{int(ledger["id"])}' if ledger.get("id") else "")
    result = request(endpoint, payload=payload, headers=headers)
    ledger.update({"id": int(result["id"]), "url": result["link"], "status": result["status"],
                   "content_hash": sha, "pending": False, "modified_gmt": result.get("modified_gmt"), "updated_at": now()})
    write_json(ledger_path, ledger)
    return ledger


def wp_reconcile(root: Path, name: str) -> dict:
    """Только чтение сервера: восстановить ledger после неопределённого ответа POST."""
    base, headers = wp_setup(root)
    path = job_path(root, name) / "wordpress.json"
    ledger = read_json(path)
    if base != ledger.get("base") or not ledger.get("pending"):
        raise BambooError("Нет незавершённой операции для этого сайта")
    if ledger.get("pending_kind") == "media":
        key = ledger["pending_media"]
        matches = request(base + "/media?" + urlencode({"slug": key, "context": "edit"}), headers=headers, readonly=True)
        found = [m for m in matches if m.get("slug") == key]
        if len(found) != 1:
            raise BambooError("Медиа не найдено однозначно; проверьте библиотеку вручную, pending сохранён")
        ledger["media"][key] = {"id": int(found[0]["id"]), "url": http_url(found[0]["source_url"], True)}
        ledger["pending"] = False
        write_json(path, ledger)
        return ledger
    matches = request(base + "/posts?" + urlencode({"slug": name, "context": "edit", "status": "any"}), headers=headers, readonly=True)
    marker = f'<!-- bamboo:{name}:{ledger["pending_hash"]} -->'
    found = [p for p in matches if marker in p.get("content", {}).get("raw", "")]
    if len(found) != 1:
        raise BambooError("Однозначное совпадение не найдено; проверьте запись вручную, pending сохранён")
    item = found[0]
    ledger.update({"pending": False, "id": item["id"], "url": item["link"], "status": item["status"], "content_hash": ledger["pending_hash"], "modified_gmt": item.get("modified_gmt")})
    write_json(path, ledger)
    return ledger
