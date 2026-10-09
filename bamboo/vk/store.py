"""Прочитать → сохранить снимок → показать план → подтвердить → записать → сверить."""
from __future__ import annotations

import copy
import hashlib
import re
import time
import uuid
from decimal import Decimal
from pathlib import Path

from .auth import token
from .common import VKError, Rejected, Uncertain, config, fingerprint, local, locked, read, safe, write
from .registry import REGISTRY, REF, PRODUCT, validate, api_params
from .transport import API


def initialize(root, community, client_id=None, redirect_uri="http://localhost/vk/callback"):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", community):
        raise VKError("Укажите короткое имя сообщества, например bamboopottery, без URL")
    path = local(root, "config.json")
    if path.exists():
        raise VKError("Конфигурация уже существует; она не перезаписана")
    write(path, {"schema_version": 1, "community": community, "group_id": None,
                 "client_id": str(client_id) if client_id else None, "redirect_uri": redirect_uri,
                 "scopes": "wall photos groups market", "api_version": "5.199"})
    ignore = safe(root, ".gitignore")
    text = ignore.read_text(encoding="utf-8") if ignore.exists() else ""
    if ".bamboo/vk/" not in text.splitlines():
        ignore.write_text(text.rstrip() + "\n.bamboo/vk/\n", encoding="utf-8")
    return {"created": True, "network_called": False, "next": "vk login либо vk token-import, затем vk check"}


def items(response):
    if isinstance(response, list):
        return response
    if isinstance(response, dict) and isinstance(response.get("items"), list):
        return response["items"]
    raise VKError("Неожиданная структура списка VK")


def stable(entity, obj):
    if obj is None:
        return None
    if not isinstance(obj, dict):
        raise VKError("Неожиданная структура объекта VK")
    if entity == "product":
        keep = set(PRODUCT) | {"id", "owner_id", "title", "albums_ids", "is_deleted", "availability", "price", "category", "dimensions", "photos", "albums", "thumb_photo"}
    elif entity == "post":
        keep = {"id", "owner_id", "is_deleted", "from_id", "text", "date", "attachments", "is_pinned", "copyright", "signer_id"}
    elif entity == "album":
        keep = {"id", "owner_id", "is_deleted", "title", "photo", "is_main", "is_hidden", "main_album"}
    else:
        keep = set(obj) - {"public_category_list", "subject_list"}
    return {key: copy.deepcopy(obj[key]) for key in sorted(keep) if key in obj}


def target_key(name, params):
    entity = REGISTRY[name]["entity"]
    field = {"product": "item_id", "post": "post_id", "album": "album_id"}.get(entity)
    return entity + ":" + str(params[field]) if field and field in params else ("store" if entity == "store" else None)


def attachment_ids(before):
    values = []
    for attachment in before.get("attachments", []):
        kind = attachment.get("type")
        value = attachment.get(kind, {})
        if kind not in ("photo", "video", "doc", "market", "album", "poll") or "id" not in value or "owner_id" not in value or value.get("access_key"):
            raise VKError("Запись содержит вложение, которое нельзя безопасно пересобрать. Укажите attachments явно после проверки")
        values.append(f"{kind}{value['owner_id']}_{value['id']}")
    return values


def media(root, relative):
    path = safe(root, relative)
    if not str(relative).startswith("content/media/") or path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
        raise VKError("Фото: JPG/PNG внутри content/media/")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise VKError("Локальный предел фотографии — 20 МиБ")
    content = path.read_bytes()
    if not (content.startswith(b"\x89PNG\r\n\x1a\n") or (content.startswith(b"\xff\xd8\xff") and content.endswith(b"\xff\xd9"))):
        raise VKError("Файл не похож на JPEG/PNG")
    return content


class Store:
    def __init__(self, root, api=None):
        self.root = Path(root).resolve()
        self.cfg = config(self.root)
        self.api = api or API(lambda: token(self.root), self.cfg["api_version"])

    @property
    def group_id(self):
        if not self.cfg.get("group_id"):
            raise VKError("Сообщество ещё не связано с числовым ID; выполните vk check")
        return self.cfg["group_id"]

    def identity(self, bind=False):
        user = self.api.call("users.get")
        if not isinstance(user, list) or len(user) != 1 or type(user[0].get("id")) is not int:
            raise VKError("Не удалось подтвердить пользовательский ключ администратора")
        groups = self.api.call("groups.getById", {"group_ids": str(self.cfg.get("group_id") or self.cfg["community"]),
                                                  "fields": "description,site,market"})
        groups = groups.get("groups", []) if isinstance(groups, dict) else groups
        if not isinstance(groups, list) or len(groups) != 1 or type(groups[0].get("id")) is not int or groups[0]["id"] <= 0:
            raise VKError("Сообщество не найдено однозначно")
        group = groups[0]
        if self.cfg.get("group_id") and self.cfg["group_id"] != group["id"]:
            raise VKError("VK вернул другое сообщество; операция заблокирована")
        if bind and not self.cfg.get("group_id"):
            self.cfg["group_id"] = group["id"]
            write(local(self.root, "config.json"), self.cfg)
        return {"user_id": user[0]["id"], "group_id": group["id"], "name": group.get("name"),
                "admin_level": group.get("admin_level", 0)}

    def check(self):
        result = self.identity(bind=True)
        probes = {}
        for name in ("store.get", "product.list", "post.list"):
            try:
                self.read_operation(name, {"count": 1} if name.endswith(".list") else {})
                probes[name] = "read_ok"
            except VKError as exc:
                probes[name] = str(exc)
        result.update({"probes": probes, "write_access": "not_tested",
                       "note": "Чтение не доказывает права записи. Тестовых публикаций и товаров не создано."})
        return result

    def cache(self):
        path = local(self.root, "snapshot.json")
        value = read(path) if path.exists() else {"group_id": self.group_id, "objects": {}}
        if value.get("group_id") != self.group_id or not isinstance(value.get("objects"), dict):
            raise VKError("Снимок относится к другому сообществу")
        return value

    def read_operation(self, name, params=None, cache=True):
        params = validate(name, params or {})
        if REGISTRY[name]["write"]:
            raise VKError("Запись возможна только через утверждённый план")
        response = self.api.call(REGISTRY[name]["method"], api_params(name, params, self.group_id))
        entity = REGISTRY[name]["entity"]
        if entity:
            values = [response] if entity == "store" else items(response)
            id_key = {"product": "item_id", "post": "post_id", "album": "album_id"}.get(entity)
            for obj in values:
                if not isinstance(obj, dict) or obj.get("owner_id", -self.group_id) != -self.group_id:
                    raise VKError("VK вернул объект другого владельца")
                if name.endswith(".get") and id_key and obj.get("id") != params[id_key]:
                    raise VKError("VK вернул другой объект вместо запрошенного ID")
        if cache and entity:
            with locked(local(self.root, "snapshot.lock")):
                snapshot = self.cache()
                for obj in values:
                    if not isinstance(obj, dict) or (entity != "store" and type(obj.get("id")) is not int):
                        raise VKError("VK вернул объект без ID")
                    if entity in ("product", "post") and obj.get("owner_id", -self.group_id) != -self.group_id:
                        raise VKError("В снимок попал объект другого владельца")
                    key = "store" if entity == "store" else entity + ":" + str(obj["id"])
                    snapshot["objects"][key] = {"at": time.time(), "data": stable(entity, obj)}
                key = target_key(name, params)
                if key and not values:
                    snapshot["objects"].pop(key, None)
                write(local(self.root, "snapshot.json"), snapshot)
        return response

    def sync(self):
        self.group_id
        counts = {}
        for name in ("product.list", "album.list"):
            seen = set()
            offset = 0
            while True:
                result = self.read_operation(name, {"count": 100, "offset": offset})
                batch = items(result)
                if not isinstance(result, dict) or type(result.get("count")) is not int:
                    raise VKError("VK не вернул размер каталога")
                ids = [x["id"] for x in batch]
                if len(ids) != len(set(ids)) or seen.intersection(ids):
                    raise VKError("Каталог изменился во время чтения; повторите vk sync")
                seen.update(ids)
                offset += len(batch)
                if offset >= result["count"]:
                    break
                if not batch or offset >= 10000:
                    raise VKError("Каталог получен не полностью; используйте точечное чтение по ID")
            counts[name] = len(seen)
        self.read_operation("store.get")
        posts = self.read_operation("post.list", {"count": 100})
        counts["post.list"] = len(items(posts))
        return {"counts": counts, "posts_complete": posts.get("count", 101) <= counts["post.list"],
                "note": "Посты: последние 100. Отложенные: vk read post.list --params '{\"filter\":\"postponed\"}'. Снимок не является атомарной копией VK."}

    def current(self, name, params):
        entity = REGISTRY[name]["entity"]
        key = {"product": "item_id", "post": "post_id", "album": "album_id"}.get(entity)
        response = self.read_operation(entity + ".get", {key: params[key]} if key else {}, cache=False)
        value = response if entity == "store" else (items(response)[0] if items(response) else None)
        return stable(entity, value)

    def approved_content(self, name, fmt="vk"):
        from bamboo.core import BambooError, approval_token, job_path, read_json
        from bamboo.quality import clean, require_approval
        try:
            require_approval(self.root, name)
            pack = read_json(job_path(self.root, name) / "pack.json")
            if fmt not in pack["formats"]:
                raise VKError(f"В утверждённом пакете отсутствует формат {fmt}")
            entry = pack["formats"][fmt]
            title = clean(pack.get("title", "")) if fmt == "article" else ""
            text = clean(entry["text"])
            cta = clean(entry["cta"])
            parts = [p for p in (title, text, cta) if p]
            message = "\n\n".join(parts)
            return message, [p["path"] for p in pack["photos"]], approval_token(self.root, name)
        except BambooError as exc:
            raise VKError(str(exc)) from None

    def plan_post(self, name, request, publish_date=None, fmt="vk"):
        message, photos, _ = self.approved_content(name, fmt=fmt)
        if len(photos) > 10:
            raise VKError("VK допускает в этом сценарии до десяти вложений")
        actions = [{"operation": "photo.wall", "params": {"file": path}} for path in photos]
        params = {"message": message, "attachments": ["$" + str(i) + ".attachment" for i in range(len(photos))]}
        if publish_date is not None:
            params["publish_date"] = publish_date
        actions.append({"operation": "post.create", "params": params, "content_job": name, "content_format": fmt})
        return self.plan({"request": request, "actions": actions})

    def plan(self, request):
        if not isinstance(request, dict) or set(request) != {"request", "actions"}:
            raise VKError("План требует request и actions; других ключей нет")
        if not isinstance(request["request"], str) or not 1 <= len(request["request"]) <= 4096:
            raise VKError("Сохраните конкретное поручение пользователя в request")
        if not isinstance(request["actions"], list) or not 1 <= len(request["actions"]) <= 50:
            raise VKError("В плане от 1 до 50 операций")
        cache = self.cache()
        actions, targets = [], set()
        total_media = 0
        for index, action in enumerate(request["actions"]):
            if not isinstance(action, dict) or set(action) - {"operation", "params", "content_job", "content_format"} or "operation" not in action:
                raise VKError("Некорректная операция плана")
            name = action["operation"]
            params = validate(name, action.get("params", {}), references=True)
            if not REGISTRY[name]["write"]:
                raise VKError("В план входят только изменения; чтение выполняется отдельно")
            for key, value in params.items():
                for part in value if isinstance(value, list) else [value]:
                    match = REF.fullmatch(part) if isinstance(part, str) else None
                    if match:
                        previous = int(match[1])
                        expected = "photo.wall" if key == "attachments" else "photo.product"
                        if previous >= index or actions[previous]["operation"] != expected:
                            raise VKError("Ссылка должна указывать на предшествующую загрузку подходящей фотографии")
            target = target_key(name, params)
            before = None
            if target:
                if target in targets:
                    raise VKError("Один объект можно изменять только один раз в плане")
                targets.add(target)
                cached = cache["objects"].get(target)
                if not cached or time.time() - cached["at"] > 86400:
                    raise VKError("Для изменения нужен свежий снимок объекта: vk read или vk sync")
                before = cached["data"]
            if name == "post.update":
                if "attachments" not in params:
                    params["attachments"] = attachment_ids(before)
                if "message" not in params:
                    params["message"] = before.get("text", "")
            if "publish_date" in params and params["publish_date"] <= time.time():
                raise VKError("Отложенная публикация требует будущего Unix-времени")
            normalized = {"operation": name, "params": params, "before": before,
                          "destructive": REGISTRY[name]["destructive"] or params.get("deleted") is True}
            if name.startswith("photo."):
                content = media(self.root, params["file"])
                total_media += len(content)
                if total_media > 50 * 1024 * 1024:
                    raise VKError("Суммарно в плане допускается не более 50 МиБ фотографий")
                normalized["media_sha256"] = hashlib.sha256(content).hexdigest()
            if action.get("content_job"):
                content_fmt = action.get("content_format", "vk")
                message, photos, approval = self.approved_content(action["content_job"], fmt=content_fmt)
                expected_photos = ["$" + str(i) + ".attachment" for i in range(index)]
                if (name != "post.create" or params.get("message") != message
                        or params.get("attachments", []) != expected_photos
                        or [a["params"].get("file") for a in actions] != photos
                        or any(a["operation"] != "photo.wall" for a in actions)):
                    raise VKError("План публикации не совпадает с утверждённым пакетом; используйте vk plan-post")
                normalized["content_job"] = action["content_job"]
                if action.get("content_format"):
                    normalized["content_format"] = action["content_format"]
                normalized["content_approval"] = approval
            actions.append(normalized)
        payload = {"schema_version": 1, "id": uuid.uuid4().hex, "group_id": self.group_id,
                   "config_hash": fingerprint(self.cfg), "request": request["request"],
                   "created_at": time.time(), "expires_at": time.time() + 3600, "actions": actions}
        document = {"payload": payload, "hash": fingerprint(payload)}
        write(local(self.root, "plans/" + payload["id"] + ".json"), document)
        return self.show(payload["id"])

    def document(self, plan_id):
        if not isinstance(plan_id, str) or not re.fullmatch(r"[a-f0-9]{32}", plan_id):
            raise VKError("Некорректный ID плана")
        document = read(local(self.root, "plans/" + plan_id + ".json"))
        if set(document) != {"payload", "hash"} or fingerprint(document["payload"]) != document["hash"] or document["payload"]["id"] != plan_id:
            raise VKError("Файл плана изменён; подготовьте новый план")
        if document["payload"]["group_id"] != self.group_id or document["payload"]["config_hash"] != fingerprint(config(self.root)):
            raise VKError("Конфигурация изменилась; подготовьте новый план")
        return document

    def show(self, plan_id):
        document = self.document(plan_id)
        return {"plan_id": plan_id, "confirmation": "vk:" + plan_id + ":" + document["hash"],
                "plan": document["payload"], "network_called": False,
                "note": "Подтверждает человек после просмотра before и params. Хеш фиксирует версию, но не доказывает личность."}

    def resolve(self, params, state):
        def one(value):
            match = REF.fullmatch(value) if isinstance(value, str) else None
            if match:
                result = state["actions"][int(match[1])].get("response", {})
                if match[2] not in result:
                    raise VKError("Нет результата предшествующей загрузки")
                return result[match[2]]
            return value
        return {key: [one(x) for x in value] if isinstance(value, list) else one(value) for key, value in params.items()}

    def verify(self, name, params, response):
        if name.startswith("photo."):
            valid = (isinstance(response, dict) and type(response.get("photo_id")) is int
                     and response["photo_id"] > 0 and isinstance(response.get("attachment"), str)
                     and bool(re.fullmatch(r"photo-?\d+_" + str(response["photo_id"]), response["attachment"])))
            return {"verified": valid, "basis": "ID сохранённого фото" if valid else "Нет подтверждённого ID; требуется ручная сверка"}
        target = dict(params)
        if name.endswith(".create"):
            field, result_key = {"product.create": ("item_id", "market_item_id"), "post.create": ("post_id", "post_id"),
                                 "album.create": ("album_id", "market_album_id")}[name]
            if not isinstance(response, dict) or type(response.get(result_key)) is not int:
                return {"verified": False, "reason": "Не получен ID созданного объекта"}
            target[field] = response[result_key]
        after = self.current(name, target)
        if name.endswith(".delete"):
            return {"verified": after is None or bool(after.get("is_deleted")) or (name == "product.delete" and after.get("availability") == 1), "after": after}
        if after is None:
            return {"verified": False, "reason": "Объект пока не найден"}
        differences = []
        for key, expected in params.items():
            if key in ("item_id", "post_id", "album_id"):
                continue
            actual = after.get(key)
            if key == "name" and name.startswith("product."):
                actual = after.get("title")
            elif key == "message":
                actual = after.get("text")
                if isinstance(actual, str):
                    actual = re.sub(r"\[#alias\|[^|\]]+\|([^\]]+)\]", r"\1", actual)
            elif key in ("price", "old_price"):
                price = after.get("price", {})
                amount = price.get("amount" if key == "price" else "old_amount") if isinstance(price, dict) else None
                actual = format(Decimal(str(amount)) / 100, ".2f") if amount is not None else None
            elif key == "category_id":
                actual = after.get("category", {}).get("id")
            elif key == "deleted":
                actual = after.get("availability") == 1 if "availability" in after else None
            elif key == "main_photo_id":
                photos = after.get("photos", [])
                actual = photos[0].get("id") if photos else None
            elif key == "photo_ids":
                actual = [p["id"] for p in after.get("photos", [])[1:]]
            elif key == "attachments":
                actual = attachment_ids(after)
            elif key == "publish_date":
                actual = after.get("date")
            elif key == "photo_id":
                actual = after.get("photo", {}).get("id")
            elif key == "main_album":
                actual = after.get("main_album", after.get("is_main"))
            elif key.startswith("dimension_"):
                actual = after.get("dimensions", {}).get(key.removeprefix("dimension_"))
            elif name == "store.update" and (key == "market" or key.startswith("market_")) and isinstance(after.get("market"), dict):
                info = after["market"]
                actual = info.get("enabled" if key == "market" else key.removeprefix("market_"))
                if isinstance(actual, dict):
                    actual = actual.get("id")
            if actual != expected:
                differences.append({"field": key, "expected": expected, "actual": actual})
        if name in ("post.pin", "post.unpin") and bool(after.get("is_pinned")) != (name == "post.pin"):
            differences.append({"field": "is_pinned"})
        return {"verified": not differences, "differences": differences, "after": after}

    def apply(self, plan_id, confirmation=None, execute=False):
        if type(execute) is not bool:
            raise VKError("execute должен быть логическим значением")
        document = self.document(plan_id)
        if not execute:
            return self.show(plan_id)
        expected = "vk:" + plan_id + ":" + document["hash"]
        if confirmation != expected:
            raise VKError("Нужно явное подтверждение человеком полной версии плана")
        payload = document["payload"]
        state_path = local(self.root, "receipts/" + plan_id + ".json")
        with locked(local(self.root, "apply.lock")):
            state = read(state_path) if state_path.exists() else {"plan_hash": document["hash"], "actions": [{"status": "pending"} for _ in payload["actions"]]}
            if (state.get("plan_hash") != document["hash"] or not isinstance(state.get("actions"), list)
                    or len(state["actions"]) != len(payload["actions"])
                    or any(not isinstance(r, dict) or "status" not in r for r in state["actions"])):
                raise VKError("Журнал повреждён или относится к другой версии плана")
            done = {"verified"}
            if all(record["status"] in done for record in state["actions"]):
                return {"already_applied": True, "receipt": state}
            if any(record["status"] not in done | {"pending"} for record in state["actions"]):
                raise VKError("Повтор заблокирован. Выполните vk reconcile; не создавайте дубликат вслепую")
            if payload["expires_at"] < time.time():
                raise VKError("План старше часа; обновите снимок и подтверждение")
            if self.identity()["admin_level"] < 2:
                raise VKError("Пользовательский ключ не подтвердил права редактора/администратора")
            media_buffers = {}
            # Проверить все условия ДО первой записи.
            for index, action in enumerate(payload["actions"]):
                if state["actions"][index]["status"] in done:
                    continue
                validate(action["operation"], action["params"], references=True)
                if action.get("content_job"):
                    _, _, approval = self.approved_content(action["content_job"], fmt=action.get("content_format", "vk"))
                    if approval != action["content_approval"]:
                        raise VKError("Контент-пакет изменился после подготовки плана")
                if action["before"] is not None and self.current(action["operation"], action["params"]) != action["before"]:
                    raise VKError("Объект изменился в VK; обновите снимок и подготовьте новый план")
                if "media_sha256" in action:
                    content = media(self.root, action["params"]["file"])
                    if hashlib.sha256(content).hexdigest() != action["media_sha256"]:
                        raise VKError("Фото изменилось после подготовки плана")
                    media_buffers[index] = content
            write(state_path, state)
            for index, action in enumerate(payload["actions"]):
                record = state["actions"][index]
                if record["status"] in done:
                    continue
                name = action["operation"]
                params = validate(name, self.resolve(action["params"], state))
                if action["before"] is not None and self.current(name, params) != action["before"]:
                    record.update(status="conflict", error="Объект изменился перед записью")
                    write(state_path, state)
                    return {"ok": False, "receipt": state}
                if "publish_date" in params and params["publish_date"] <= time.time():
                    raise VKError("Время отложенной публикации уже наступило; нужен новый план")
                record.update(status="sending", operation=name, params=params, started_at=time.time())
                write(state_path, state)  # Сбой после этой точки запрещает автоматический повтор.
                try:
                    if name.startswith("photo."):
                        response = self.api.upload(name, media_buffers[index], params["file"], self.group_id, params.get("main_photo", True))
                    else:
                        outgoing = api_params(name, params, self.group_id)
                        if name == "post.create":
                            outgoing["guid"] = plan_id + "-" + str(index)
                        response = self.api.call(REGISTRY[name]["method"], outgoing, mutation=True)
                    record.update(status="written", response=response)
                    write(state_path, state)
                    verification = self.verify(name, params, response)
                    record.update(status="verified" if verification["verified"] else "unverified", verification=verification)
                except Rejected as exc:
                    record.update(status="rejected", error=str(exc))
                except (VKError, OSError, ValueError, KeyError, TypeError, AttributeError):
                    record.update(status="unverified" if record["status"] == "written" else "uncertain",
                                  error="Исход записи или сверки не подтверждён; повтор запрещён")
                write(state_path, state)
                if record["status"] not in done:
                    return {"ok": False, "receipt": state, "next": "vk reconcile " + plan_id}
            return {"ok": True, "receipt": state}

    def reconcile(self, plan_id):
        document = self.document(plan_id)
        state = read(local(self.root, "receipts/" + plan_id + ".json"))
        findings = []
        for index, record in enumerate(state["actions"]):
            action = document["payload"]["actions"][index]
            if record["status"] == "pending":
                findings.append({"index": index, "status": "not_sent"})
                continue
            if action["operation"].endswith(".create") and not record.get("response"):
                findings.append({"index": index, "status": "manual_lookup_required", "reason": "Нет ID; поиск по похожему тексту не доказывает выполнение"})
                continue
            try:
                verification = self.verify(action["operation"], record.get("params", action["params"]), record.get("response", {}))
                findings.append({"index": index, "verification": verification})
            except (VKError, ValueError, TypeError, KeyError, OSError):
                findings.append({"index": index, "status": "not_confirmed"})
        return {"findings": findings, "writes_performed": False,
                "note": "Сверка не разблокирует повтор и не является откатом. Сопоставьте результат; новые изменения — отдельный свежий план."}
