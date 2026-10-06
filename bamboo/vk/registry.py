"""Ограниченный реестр VK API 5.199: произвольные методы агенту не доступны."""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from .common import VKError

# Правила: тип, минимум, максимум. Деньги задаются в основных единицах валюты.
ID = ("int", 1, 2**63 - 1)
ZERO = ("int", 0, 2**63 - 1)
BOOL = ("bool",)
TEXT = ("str", 0, 16384)
PRICE = ("money", 0, 999999999999)
PRODUCT = {
    "name": ("str", 4, 100), "description": ("str", 10, 16384),
    "category_id": ZERO, "price": PRICE, "old_price": PRICE, "deleted": BOOL,
    "main_photo_id": ZERO, "photo_ids": ("ids", 0, 4), "video_ids": ("ids", 0, 2),
    "variant_ids": ("ids", 0, 2), "is_main_variant": BOOL,
    "url": ("url", 0, 320), "sku": ("str", 0, 50),
    "stock_amount": ("int", -1, 999999), "weight": ("int", 0, 100000000),
    **{key: ("int", 0, 100000) for key in ("dimension_width", "dimension_height", "dimension_length")},
}
STORE = {
    "title": ("str", 1, 100), "description": TEXT, "website": ("url", 0, 320),
    "screen_name": ("str", 5, 100), "market": BOOL, "market_comments": BOOL,
    "market_country": ("ids", 0, 10), "market_city": ("ids", 0, 10),
    "market_currency": ("currency",), "market_contact": ZERO, "market_wiki": ZERO,
    "messages": BOOL, "articles": BOOL, "addresses": BOOL, "contacts": BOOL,
    "wall": ("int", 0, 3), "main_section": ZERO, "secondary_section": ZERO,
    **{key: ("int", 0, 2) for key in ("photos", "video", "topics", "docs", "wiki")},
}
POST = {"message": TEXT, "attachments": ("attachments", 0, 10),
        "publish_date": ZERO, "signed": BOOL, "close_comments": BOOL,
        "copyright": ("url", 0, 320)}
ALBUM = {"title": ("str", 1, 128), "photo_id": ZERO, "main_album": BOOL, "is_hidden": BOOL}
PAGING = {"count": ("int", 1, 100), "offset": ("int", 0, 1000000)}
REGISTRY = {}


def register(name, method, entity, fields=None, required=(), write=False, destructive=False):
    REGISTRY[name] = {"method": method, "entity": entity, "fields": fields or {},
                      "required": list(required), "write": write, "destructive": destructive}


register("product.list", "market.get", "product", {**PAGING, "album_id": ZERO})
register("product.get", "market.getById", "product", {"item_id": ID}, ("item_id",))
register("product.search", "market.search", "product", {**PAGING, "q": ("str", 1, 200)})
register("product.create", "market.add", "product", PRODUCT,
         ("name", "description", "category_id", "price", "main_photo_id"), True)
register("product.update", "market.edit", "product", {"item_id": ID, **PRODUCT}, ("item_id",), True)
register("product.delete", "market.delete", "product", {"item_id": ID}, ("item_id",), True, True)
register("album.list", "market.getAlbums", "album", PAGING)
register("album.get", "market.getAlbumById", "album", {"album_id": ID}, ("album_id",))
register("album.create", "market.addAlbum", "album", ALBUM, ("title",), True)
register("album.update", "market.editAlbum", "album", {"album_id": ID, **ALBUM}, ("album_id", "title"), True)
register("album.delete", "market.deleteAlbum", "album", {"album_id": ID}, ("album_id",), True, True)
register("post.list", "wall.get", "post", {**PAGING, "filter": ("enum", "owner", "postponed")})
register("post.get", "wall.getById", "post", {"post_id": ID}, ("post_id",))
register("post.create", "wall.post", "post", POST, (), True)
register("post.update", "wall.edit", "post", {"post_id": ID, **POST}, ("post_id",), True)
register("post.delete", "wall.delete", "post", {"post_id": ID}, ("post_id",), True, True)
register("post.pin", "wall.pin", "post", {"post_id": ID}, ("post_id",), True)
register("post.unpin", "wall.unpin", "post", {"post_id": ID}, ("post_id",), True)
register("store.get", "groups.getSettings", "store")
register("store.update", "groups.edit", "store", STORE, (), True, True)
register("categories", "market.getCategories", None, {**PAGING})
register("statistics", "stats.get", None, {"timestamp_from": ZERO, "timestamp_to": ZERO,
                                           "interval": ("enum", "day", "week", "month", "year", "all")})
register("photo.product", "photos.saveMarketPhoto", "photo", {"file": ("file",), "main_photo": BOOL}, ("file",), True)
register("photo.wall", "photos.saveWallPhoto", "photo", {"file": ("file",)}, ("file",), True)
REF = re.compile(r"^\$(\d+)\.(photo_id|attachment)$")


def value_check(value, rule):
    kind = rule[0]
    if kind in ("int", "bool"):
        expected = int if kind == "int" else bool
        valid = type(value) is expected and (kind == "bool" or rule[1] <= value <= rule[2])
    elif kind == "money":
        try:
            number = Decimal(str(value))
            valid = not isinstance(value, bool) and number.is_finite() and rule[1] <= number <= rule[2] and number == number.quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            valid = False
    elif kind in ("ids", "attachments"):
        valid = isinstance(value, list) and rule[1] <= len(value) <= rule[2]
        if valid:
            valid = all((type(x) is int and x >= 0) if kind == "ids" else
                        (isinstance(x, str) and bool(re.fullmatch(r"(?:photo|video|doc|market|album|poll)-?\d+_\d+", x))) for x in value)
    elif kind == "currency":
        valid = type(value) is int and value in (643, 980, 398, 978, 840)
    elif kind == "enum":
        valid = value in rule[1:]
    elif kind == "file":
        valid = isinstance(value, str) and value.startswith("content/media/")
    else:
        valid = isinstance(value, str) and rule[1] <= len(value) <= rule[2]
        if kind == "url" and value:
            from urllib.parse import urlsplit
            url = urlsplit(value)
            valid = valid and url.scheme == "https" and bool(url.hostname) and not url.username and not url.password
    if not valid:
        raise VKError("Параметр не соответствует контракту операции")
    return format(Decimal(str(value)), ".2f") if kind == "money" else value


def validate(name, params, references=False):
    if name not in REGISTRY:
        raise VKError("Операция не поддерживается; используйте vk operations")
    spec = REGISTRY[name]
    if not isinstance(params, dict) or set(params) - spec["fields"].keys():
        raise VKError("Неизвестные параметры; owner_id, group_id, токены и произвольные методы задавать нельзя")
    if set(spec["required"]) - params.keys():
        raise VKError("Отсутствуют обязательные параметры: " + ", ".join(sorted(set(spec["required"]) - params.keys())))
    normalized = {}
    for key, value in params.items():
        rule = spec["fields"][key]
        values = value if isinstance(value, list) else [value]
        has_ref = any(isinstance(x, str) and REF.fullmatch(x) for x in values)
        if has_ref and references and key in ("main_photo_id", "photo_ids", "attachments"):
            if isinstance(value, list) != (rule[0] in ("ids", "attachments")):
                raise VKError("Скалярное поле и список ссылок несовместимы")
            if isinstance(value, list) and len(value) > rule[2]:
                raise VKError("Слишком много вложений")
            for x in values:
                if isinstance(x, str) and REF.fullmatch(x):
                    field = REF.fullmatch(x)[2]
                    if field != ("attachment" if key == "attachments" else "photo_id"):
                        raise VKError("Неверный тип ссылки на результат загрузки")
                else:
                    value_check([x] if isinstance(value, list) else x, rule)
            normalized[key] = value
        else:
            normalized[key] = value_check(value, rule)
    if name in ("product.update", "post.update") and len(normalized) < 2:
        raise VKError("Не указаны изменения")
    if name == "store.update" and not normalized:
        raise VKError("Не указаны изменения")
    if name == "post.create" and not (normalized.get("message") or normalized.get("attachments")):
        raise VKError("Нужны текст или вложения")
    return normalized


def api_params(name, params, group_id):
    p = dict(params)
    method = REGISTRY[name]["method"]
    if name == "product.get":
        p = {"item_ids": f"-{group_id}_{p['item_id']}", "extended": 1}
    elif name == "album.get":
        p = {"owner_id": -group_id, "album_ids": str(p["album_id"])}
    elif name == "post.get":
        p = {"posts": f"-{group_id}_{p['post_id']}"}
    elif method.startswith("groups.") or name == "categories":
        p["group_id"] = group_id
    elif name == "statistics":
        p["group_id"] = group_id
    else:
        p["owner_id"] = -group_id
    if name in ("product.list", "product.search"):
        p["extended"] = 1
    if name == "post.create":
        p["from_group"] = 1
    return p
