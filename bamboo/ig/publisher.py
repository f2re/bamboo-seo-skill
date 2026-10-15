"""План и публикация Reels из утверждённого контент-пакета.

Порядок: видео → Yandex Object Storage → временная ссылка → контейнер REELS → ожидание
обработки → media_publish → проверка записи → удаление временного файла.
Публикация возобновляема: один контейнер Instagram не публикуется дважды, а перед повтором
media_publish проверяется status_code контейнера.
"""
from __future__ import annotations

import hashlib
import time
import uuid

from ..vk.common import fingerprint, locked, read, safe, write
from .errors import IGError, Rejected, Uncertain
from .graph import Graph
from .storage import Storage
from . import secrets

PLAN_TTL = 3600
LINK_TTL = 2 * 3600
CAPTION_LIMIT, HASHTAG_LIMIT = 2200, 5


def local(root, name):
    return safe(root, ".bamboo/ig/" + name)


def initialize(root, bucket, prefix="bamboo-ig/", endpoint="https://storage.yandexcloud.net", region="ru-central1"):
    if not prefix.endswith("/") or prefix.startswith("/") or ".." in prefix:
        raise IGError("Префикс вида папка/")
    Storage("check", "check", bucket, endpoint, region)  # проверка формата без сети
    data = {"schema_version": 1, "api_version": "v25.0", "bucket": bucket, "prefix": prefix,
            "endpoint": endpoint, "region": region}
    write(local(root, "config.json"), data)
    return {"saved": True, "config": data, "network_called": False,
            "next": "ig token-import, затем ig storage-key-import и ig check"}


def config(root):
    path = local(root, "config.json")
    if not path.exists():
        raise IGError("Сначала выполните ig init --bucket ИМЯ")
    value = read(path)
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise IGError("Некорректная настройка .bamboo/ig/config.json")
    return value


def approved_reel(root, job):
    """Подпись и видео ровно той версии, которую утвердил владелец."""
    from ..core import BambooError, approval_token, file_digest, job_path, read_json
    from ..quality import clean, require_approval
    try:
        require_approval(root, job)
        pack = read_json(job_path(root, job) / "pack.json")
        entry = pack["formats"].get("instagram")
        if not entry:
            raise IGError("В утверждённом пакете нет формата instagram")
        videos = pack.get("videos", [])
        path = entry.get("video") or (videos[0]["path"] if len(videos) == 1 else None)
        if not path or path not in [v["path"] for v in videos]:
            raise IGError("Укажите formats.instagram.video из pack.videos")
        caption = "\n\n".join(x for x in (clean(entry["text"]), clean(entry["cta"])) if x)
        return caption, path, file_digest(safe(root, path)), approval_token(root, job)
    except BambooError as exc:
        raise IGError(str(exc)) from None


def check_caption(caption):
    if len(caption) > CAPTION_LIMIT:
        raise IGError(f"Подпись Instagram длиннее {CAPTION_LIMIT} знаков")
    if sum(1 for word in caption.split() if word.startswith("#")) > HASHTAG_LIMIT:
        raise IGError(f"Instagram принимает не больше {HASHTAG_LIMIT} хэштегов")


class Publisher:
    def __init__(self, root, graph=None, storage=None, clock=time.time):
        self.root, self.clock = root, clock
        self.cfg = config(root)
        self._graph, self._storage = graph, storage

    @property
    def graph(self):
        if self._graph is None:
            self._graph = Graph(secrets.token(self.root), self.cfg["api_version"])
        return self._graph

    @property
    def storage(self):
        if self._storage is None:
            key_id, secret = secrets.storage_keys(self.root)
            self._storage = Storage(key_id, secret, self.cfg["bucket"], self.cfg["endpoint"], self.cfg["region"])
        return self._storage

    def check(self):
        me = self.graph.me()
        limit = self.graph.publishing_limit(me["user_id"])
        probe = self.cfg["prefix"] + "bamboo-access-check"
        self.storage.exists(probe)  # 404 = ключи и бакет в порядке, 403 = нет прав
        return {"username": me.get("username"), "user_id": me["user_id"], "account_type": me.get("account_type"),
                "publishing_limit": limit, "storage": "ok", "write_access": "not_tested",
                "note": "Тестовых публикаций и файлов не создано"}

    def plan_post(self, job, request):
        if not isinstance(request, str) or not request.strip():
            raise IGError("Сохраните поручение владельца в --request")
        caption, video, sha, approval = approved_reel(self.root, job)
        check_caption(caption)
        payload = {"schema_version": 1, "id": uuid.uuid4().hex, "request": request.strip(), "job": job,
                   "approval": approval, "video": video, "video_sha256": sha, "caption": caption,
                   "share_to_feed": True, "config_hash": fingerprint(self.cfg),
                   "created_at": self.clock(), "expires_at": self.clock() + PLAN_TTL}
        write(local(self.root, f"plans/{payload['id']}.json"), {"payload": payload, "hash": fingerprint(payload)})
        return self.show(payload["id"])

    def document(self, plan_id):
        if not isinstance(plan_id, str) or len(plan_id) != 32 or not all(c in "0123456789abcdef" for c in plan_id):
            raise IGError("Некорректный ID плана")
        doc = read(local(self.root, f"plans/{plan_id}.json"))
        if set(doc) != {"payload", "hash"} or fingerprint(doc["payload"]) != doc["hash"]:
            raise IGError("Файл плана изменён; подготовьте новый план")
        if doc["payload"]["config_hash"] != fingerprint(self.cfg):
            raise IGError("Настройка изменилась; подготовьте новый план")
        return doc

    def show(self, plan_id):
        doc = self.document(plan_id)
        p = doc["payload"]
        return {"plan_id": plan_id, "confirmation": f"ig:{plan_id}:{doc['hash']}", "job": p["job"],
                "video": p["video"], "caption": p["caption"], "share_to_feed": p["share_to_feed"],
                "network_called": False}

    def _video(self, payload):
        content = safe(self.root, payload["video"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != payload["video_sha256"]:
            raise IGError("Видео изменилось после подготовки плана")
        return content

    def apply(self, plan_id, confirmation=None, execute=False):
        doc = self.document(plan_id)
        if not execute:
            return self.show(plan_id)
        if confirmation != f"ig:{plan_id}:{doc['hash']}":
            raise IGError("Нужно подтверждение именно этой версии плана")
        payload, path = doc["payload"], local(self.root, f"receipts/{plan_id}.json")
        with locked(local(self.root, "apply.lock")):
            state = read(path) if path.exists() else {"plan_hash": doc["hash"], "stage": "pending"}
            if state.get("plan_hash") != doc["hash"]:
                raise IGError("Журнал относится к другой версии плана")
            if state["stage"] == "done":
                return {"ok": True, "already_published": True, "receipt": state}
            try:
                return self._run(payload, state, path)
            finally:
                write(path, state)

    def _run(self, payload, state, path):
        if state["stage"] == "pending":
            if payload["expires_at"] < self.clock():
                raise IGError("План старше часа; подготовьте новый")
            if approved_reel(self.root, payload["job"])[3] != payload["approval"]:
                raise IGError("Контент-пакет изменился после подготовки плана")
        user_id = state.get("user_id") or self.graph.me()["user_id"]
        state["user_id"] = user_id
        if not state.get("object_key"):
            key = self.cfg["prefix"] + payload["video_sha256"][:24] + ".mp4"
            self.storage.put(key, self._video(payload))
            state.update(object_key=key, stage="uploaded")
            write(path, state)
        if not state.get("container_id"):
            url = self.storage.presign_get(state["object_key"], LINK_TTL)
            state.update(stage="creating_container")
            write(path, state)
            state.update(container_id=self.graph.create_reel(user_id, url, payload["caption"], payload["share_to_feed"]),
                         stage="container")
            write(path, state)
        status = self.graph.wait(state["container_id"])
        state["container_status"] = status
        if status == "IN_PROGRESS":
            return {"ok": False, "stage": "processing", "receipt": state,
                    "next": "Instagram ещё обрабатывает видео; повторите ig apply с тем же подтверждением"}
        if status in ("ERROR", "EXPIRED"):
            state.update(stage="pending", container_id=None)
            raise Rejected(f"Instagram не принял видео (status_code={status}); проверьте формат ролика")
        if status == "FINISHED":
            state.update(stage="publishing")
            write(path, state)
            try:
                state["media_id"] = self.graph.publish(user_id, state["container_id"])
            except Uncertain:
                state.update(stage="uncertain")
                return {"ok": False, "stage": "uncertain", "receipt": state,
                        "next": "Повторите ig apply: перед публикацией будет проверен статус того же контейнера"}
        elif status != "PUBLISHED":
            raise IGError(f"Неизвестный статус контейнера: {status}")
        if not state.get("media_id"):
            state["media_id"] = self._find_media(user_id, payload["caption"])
        return self._finish(state)

    def _find_media(self, user_id, caption):
        for item in self.graph.recent(user_id):
            if (item.get("caption") or "").strip() == caption.strip():
                return item["id"]
        raise Uncertain("Контейнер опубликован, но запись не найдена среди последних; проверьте профиль")

    def _finish(self, state):
        media = self.graph.media(state["media_id"])
        state.update(stage="done", permalink=media.get("permalink"), media_product_type=media.get("media_product_type"),
                     published_at=media.get("timestamp"))
        try:
            self.storage.delete(state["object_key"])
            state["cleanup"] = "deleted"
        except IGError:
            state["cleanup"] = "not_deleted: удалите временный файл в бакете вручную"
        return {"ok": True, "permalink": state.get("permalink"), "receipt": state}
