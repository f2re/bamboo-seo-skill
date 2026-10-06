"""Контрактные тесты: без реальных ключей, аккаунтов и сетевых запросов."""
from __future__ import annotations

import copy
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bamboo.vk import auth
from bamboo.vk.common import VKError, Uncertain, Rejected, config, local, read, write, loads, safe
from bamboo.vk.registry import validate, api_params, REGISTRY
from bamboo.vk.store import Store, initialize
from bamboo.vk.transport import API, NoRedirect
from bamboo.vk.mcp import serve, call


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.writes = []
        self.level = 3
        self.fail_after = False
        self.reject = False
        self.wrong_readback = False
        self.products = {1: {"id": 1, "owner_id": -77, "title": "Чаша тестовая", "description": "Только тестовое описание",
                             "category": {"id": 100}, "price": {"amount": "250000", "currency": {"id": 643}},
                             "photos": [{"id": 90}], "availability": 0, "stock_amount": 2}}
        self.posts = {2: {"id": 2, "owner_id": -77, "text": "Тестовый текст", "date": 1700000000,
                          "attachments": [{"type": "photo", "photo": {"id": 90, "owner_id": -77}}]}}
        self.albums = {3: {"id": 3, "title": "Тестовая подборка"}}
        self.settings = {"title": "Тестовое сообщество", "description": "Тест", "market": {"enabled": True, "currency": {"id": 643}}}

    def call(self, method, params=None, mutation=False):
        p = params or {}
        self.calls.append((method, copy.deepcopy(p), mutation))
        if method == "users.get":
            return [{"id": 123}]
        if method == "groups.getById":
            return {"groups": [{"id": 77, "admin_level": self.level, "name": "Тест"}]}
        if method == "groups.getSettings":
            return copy.deepcopy(self.settings)
        if method in ("market.get", "market.search", "market.getAlbums", "wall.get"):
            source = self.products if method in ("market.get", "market.search") else self.albums if method == "market.getAlbums" else self.posts
            values = list(source.values())
            return {"count": len(values), "items": copy.deepcopy(values[p.get("offset", 0):p.get("offset", 0) + p.get("count", 100)])}
        if method in ("market.getById", "market.getAlbumById", "wall.getById"):
            field = "item_ids" if method == "market.getById" else "album_ids" if method == "market.getAlbumById" else "posts"
            ident = int(str(p[field]).split("_")[-1])
            source = self.products if method == "market.getById" else self.albums if method == "market.getAlbumById" else self.posts
            values = [source[ident]] if ident in source else []
            return {"count": len(values), "items": copy.deepcopy(values)}
        if mutation:
            self.writes.append((method, copy.deepcopy(p)))
            if self.reject:
                raise Rejected("VK 15: нет доступа")
            if method == "market.edit":
                target = self.products[p["item_id"]]
                for key, value in p.items():
                    if key == "price" and not self.wrong_readback:
                        target["price"]["amount"] = str(int(float(value) * 100))
                    elif key == "name":
                        target["title"] = value
                    elif key not in ("owner_id", "item_id", "price"):
                        target[key] = value
                response = 1
            elif method == "market.add":
                ident = 10
                self.products[ident] = {"id": ident, "owner_id": p["owner_id"], "title": p["name"], "description": p["description"],
                                       "category": {"id": p["category_id"]}, "price": {"amount": str(int(float(p["price"]) * 100))},
                                       "photos": [{"id": p["main_photo_id"]}]}
                response = {"market_item_id": ident}
            elif method == "wall.post":
                ident = 11
                self.posts[ident] = {"id": ident, "owner_id": p["owner_id"], "text": p.get("message", ""), "attachments": [], "date": p.get("publish_date", int(time.time()))}
                for a in p.get("attachments", []):
                    owner, photo_id = a.removeprefix("photo").split("_")
                    self.posts[ident]["attachments"].append({"type": "photo", "photo": {"id": int(photo_id), "owner_id": int(owner)}})
                response = {"post_id": ident}
            elif method == "wall.edit":
                self.posts[p["post_id"]]["text"] = p.get("message", "")
                response = 1
            elif method == "market.delete":
                del self.products[p["item_id"]]
                response = 1
            elif method == "groups.edit":
                self.settings.update({k: v for k, v in p.items() if k != "group_id"})
                response = 1
            elif method == "market.editAlbum":
                self.albums[p["album_id"]]["title"] = p["title"]
                response = 1
            else:
                raise AssertionError(method)
            if self.fail_after:
                raise Uncertain("Ответ потерян")
            return response
        raise AssertionError(method)

    def upload(self, kind, content, filename, group_id, main_photo=True):
        self.writes.append((kind, {"group_id": group_id}))
        return {"photo_id": 101, "attachment": "photo-77_101"}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve() / "project"
        self.root.mkdir()
        initialize(self.root, "test_shop", "123")
        self.api = FakeAPI()
        self.store = Store(self.root, self.api)
        self.store.check()
        self.store.sync()

    def tearDown(self):
        self.temporary.cleanup()

    def plan(self, name="product.update", params=None):
        return self.store.plan({"request": "Явное тестовое поручение", "actions": [{"operation": name, "params": params or {"item_id": 1, "price": "2700"}}]})

    def apply(self, plan):
        return self.store.apply(plan["plan_id"], plan["confirmation"], True)

    def test_plan_and_default_apply_offline(self):
        before = len(self.api.calls)
        p = self.plan()
        self.store.apply(p["plan_id"])
        self.assertEqual(len(self.api.calls), before)
        self.assertEqual(self.api.writes, [])

    def test_real_vk_title_field(self):
        p = self.plan(params={"item_id": 1, "name": "Новое название"})
        self.assertTrue(self.apply(p)["ok"])
        self.assertEqual(self.api.products[1]["title"], "Новое название")

    def test_photo_missing_receipt_not_verified(self):
        self.assertFalse(self.store.verify("photo.wall", {}, {})["verified"])

    def test_config_edit_invalidates_persistent_store(self):
        p = self.plan()
        cfg = config(self.root)
        cfg["community"] = "different"
        write(local(self.root, "config.json"), cfg)
        with self.assertRaises(VKError):
            self.apply(p)
        self.assertFalse(self.api.writes)

    def test_scalar_photo_cannot_be_list_reference(self):
        with self.assertRaises(VKError):
            validate("product.update", {"item_id": 1, "main_photo_id": ["$0.photo_id"]}, references=True)

    def test_scalar_attachment_cannot_replace_list(self):
        with self.assertRaises(VKError):
            validate("post.create", {"attachments": "$0.attachment"}, references=True)

    def test_bad_journal_length(self):
        p = self.plan()
        document = self.store.document(p["plan_id"])
        write(local(self.root, "receipts/" + p["plan_id"] + ".json"), {"plan_hash": document["hash"], "actions": []})
        with self.assertRaises(VKError):
            self.apply(p)
        self.assertFalse(self.api.writes)

    def test_wrong_object_read_rejected_even_without_cache(self):
        self.api.products[1]["id"] = 99
        with self.assertRaises(VKError):
            self.store.current("product.update", {"item_id": 1})

    def test_wrong_owner_read_rejected_even_without_cache(self):
        self.api.products[1]["owner_id"] = -78
        with self.assertRaises(VKError):
            self.store.current("product.update", {"item_id": 1})

    def test_post_text_preserved_when_only_attachments_change(self):
        p = self.plan("post.update", {"post_id": 2, "attachments": ["photo-77_90"]})
        self.assertEqual(p["plan"]["actions"][0]["params"]["message"], "Тестовый текст")
        self.assertTrue(self.apply(p)["ok"])

    def test_config_rejects_accidental_secret_field(self):
        cfg = config(self.root)
        cfg["access_token"] = "do-not-leak"
        write(local(self.root, "config.json"), cfg)
        with self.assertRaises(VKError) as caught:
            config(self.root)
        self.assertNotIn("do-not-leak", str(caught.exception))

    def test_approved_post_bridge_and_changed_approval(self):
        with patch.object(Store, "approved_content", return_value=("Согласованный текст", [], "approval-one")):
            p = self.store.plan_post("approved-job", "Опубликовать согласованный материал")
        with patch.object(Store, "approved_content", return_value=("Другой текст", [], "approval-two")):
            with self.assertRaises(VKError):
                self.apply(p)
        self.assertFalse(self.api.writes)

    def test_approved_post_cannot_be_substituted(self):
        with patch.object(Store, "approved_content", return_value=("Согласованный текст", [], "approval-one")):
            with self.assertRaises(VKError):
                self.store.plan({"request": "Публикация", "actions": [{"operation": "post.create",
                    "params": {"message": "Не согласованный текст"}, "content_job": "approved-job"}]})

    def test_price_patch_and_idempotent_replay(self):
        p = self.plan()
        result = self.apply(p)
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.writes[0], ("market.edit", {"item_id": 1, "price": "2700.00", "owner_id": -77}))
        self.assertEqual(self.api.products[1]["title"], "Чаша тестовая")
        self.assertTrue(self.apply(p)["already_applied"])
        self.assertEqual(len(self.api.writes), 1)

    def test_wrong_confirmation_no_network(self):
        p = self.plan()
        before = len(self.api.calls)
        with self.assertRaises(VKError):
            self.store.apply(p["plan_id"], "да", True)
        self.assertEqual(len(self.api.calls), before)

    def test_remote_conflict_stops_every_write(self):
        p = self.plan()
        self.api.products[1]["title"] = "Изменено владельцем"
        with self.assertRaises(VKError):
            self.apply(p)
        self.assertFalse(self.api.writes)

    def test_counters_do_not_cause_conflict(self):
        p = self.plan()
        self.api.products[1]["likes"] = {"count": 99}
        self.assertTrue(self.apply(p)["ok"])

    def test_uncertain_result_cannot_be_replayed(self):
        p = self.plan()
        self.api.fail_after = True
        result = self.apply(p)
        self.assertFalse(result["ok"])
        self.assertEqual(result["receipt"]["actions"][0]["status"], "uncertain")
        with self.assertRaises(VKError):
            self.apply(p)
        self.assertEqual(len(self.api.writes), 1)
        self.assertFalse(self.store.reconcile(p["plan_id"])["writes_performed"])
        self.assertEqual(len(self.api.writes), 1)

    def test_crash_marker_blocks_retry(self):
        p = self.plan()
        doc = self.store.document(p["plan_id"])
        write(local(self.root, "receipts/" + p["plan_id"] + ".json"), {"plan_hash": doc["hash"], "actions": [{"status": "sending"}]})
        with self.assertRaises(VKError):
            self.apply(p)
        self.assertFalse(self.api.writes)

    def test_rejection_not_retried(self):
        p = self.plan()
        self.api.reject = True
        self.assertEqual(self.apply(p)["receipt"]["actions"][0]["status"], "rejected")
        with self.assertRaises(VKError):
            self.apply(p)
        self.assertEqual(len(self.api.writes), 1)

    def test_unverified_readback_blocks_retry(self):
        p = self.plan()
        self.api.wrong_readback = True
        result = self.apply(p)
        self.assertFalse(result["ok"])
        self.assertEqual(result["receipt"]["actions"][0]["status"], "unverified")
        with self.assertRaises(VKError):
            self.apply(p)

    def test_admin_required(self):
        p = self.plan()
        self.api.level = 0
        with self.assertRaises(VKError):
            self.apply(p)
        self.assertFalse(self.api.writes)

    def test_tampered_plan_rejected(self):
        p = self.plan()
        path = local(self.root, "plans/" + p["plan_id"] + ".json")
        doc = read(path)
        doc["payload"]["actions"][0]["params"]["price"] = "1.00"
        write(path, doc)
        with self.assertRaises(VKError):
            self.apply(p)

    def test_post_attachments_preserved(self):
        p = self.plan("post.update", {"post_id": 2, "message": "Исправленный текст"})
        self.assertEqual(p["plan"]["actions"][0]["params"]["attachments"], ["photo-77_90"])
        self.assertTrue(self.apply(p)["ok"])

    def test_unknown_attachment_not_silently_removed(self):
        self.api.posts[2]["attachments"] = [{"type": "link", "link": {"url": "https://example.com"}}]
        self.store.read_operation("post.get", {"post_id": 2})
        with self.assertRaises(VKError):
            self.plan("post.update", {"post_id": 2, "message": "Текст"})

    def test_product_delete_verified(self):
        p = self.plan("product.delete", {"item_id": 1})
        self.assertTrue(p["plan"]["actions"][0]["destructive"])
        self.assertTrue(self.apply(p)["ok"])

    def test_store_settings_patch(self):
        p = self.plan("store.update", {"title": "Новое тестовое название"})
        self.assertTrue(self.apply(p)["ok"])
        self.assertEqual(self.api.writes[0][1]["group_id"], 77)
        self.assertNotIn("owner_id", self.api.writes[0][1])

    def test_album_update(self):
        p = self.plan("album.update", {"album_id": 3, "title": "Чайная посуда"})
        self.assertTrue(self.apply(p)["ok"])

    def test_two_edits_same_object_forbidden(self):
        with self.assertRaises(VKError):
            self.store.plan({"request": "Две правки", "actions": [{"operation": "product.update", "params": {"item_id": 1, "price": x}} for x in (20, 30)]})

    def test_multi_action_preflight_is_before_any_write(self):
        p = self.store.plan({"request": "Две правки", "actions": [
            {"operation": "product.update", "params": {"item_id": 1, "price": 20}},
            {"operation": "post.update", "params": {"post_id": 2, "message": "Новый текст"}}]})
        self.api.posts[2]["text"] = "Другой текст"
        with self.assertRaises(VKError):
            self.apply(p)
        self.assertFalse(self.api.writes)

    def test_photo_and_product_one_approval(self):
        path = self.root / "content/media/test.jpg"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xd8\xfftest\xff\xd9")
        p = self.store.plan({"request": "Тестовый товар с фотографией", "actions": [
            {"operation": "photo.product", "params": {"file": "content/media/test.jpg"}},
            {"operation": "product.create", "params": {"name": "Чаша тест", "description": "Тестовое описание изделия", "category_id": 100, "price": "2500", "main_photo_id": "$0.photo_id"}}]})
        self.assertTrue(self.apply(p)["ok"])
        self.assertEqual(self.api.writes[1][1]["main_photo_id"], 101)

    def test_photo_changed_after_plan_no_upload(self):
        path = self.root / "content/media/test.jpg"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xd8\xfftest\xff\xd9")
        p = self.plan("photo.product", {"file": "content/media/test.jpg"})
        path.write_bytes(b"\xff\xd8\xffother\xff\xd9")
        with self.assertRaises(VKError):
            self.apply(p)
        self.assertFalse(self.api.writes)

    def test_forward_reference_rejected(self):
        with self.assertRaises(VKError):
            self.plan("product.create", {"name": "Чаша тест", "description": "Тестовое описание", "category_id": 100, "price": 20, "main_photo_id": "$9.photo_id"})

    def test_unknown_method_and_foreign_owner_rejected(self):
        for name, params in (("execute", {}), ("product.update", {"item_id": 1, "price": 20, "owner_id": -8})):
            with self.subTest(name=name), self.assertRaises(VKError):
                self.plan(name, params)

    def test_no_snapshot_refuses_update(self):
        local(self.root, "snapshot.json").unlink()
        with self.assertRaises(VKError):
            self.plan()

    def test_pagination_more_than_100(self):
        for i in range(4, 209):
            self.api.products[i] = {**self.api.products[1], "id": i}
        result = self.store.sync()
        self.assertEqual(result["counts"]["product.list"], 206)

    def test_scheduled_post_past_rejected(self):
        with self.assertRaises(VKError):
            self.plan("post.create", {"message": "Текст", "publish_date": 1})

    def test_safe_paths_and_symlink(self):
        for path in ("../secret", "/etc/passwd", "content/../.env"):
            with self.assertRaises(VKError):
                safe(self.root, path)
        link = self.root / "link"
        try:
            link.symlink_to(self.root / "content")
        except OSError:
            self.skipTest("ОС не разрешает создание symlink для теста")
        with self.assertRaises(VKError):
            safe(self.root, "link/a")

    def test_mcp_does_not_accept_truthy_string(self):
        with self.assertRaises(VKError):
            call(self.root, "vk_apply", {"plan_id": "x", "execute": "false"})


class ContractTests(unittest.TestCase):
    def test_reject_invalid_money_and_bool_id(self):
        for value in (float("nan"), float("inf"), -1, "NaN", "1.001", True):
            with self.subTest(value=value), self.assertRaises(VKError):
                validate("product.update", {"item_id": 1, "price": value})
        with self.assertRaises(VKError):
            validate("product.get", {"item_id": True})

    def test_boolean_parameters_are_not_strings(self):
        with self.assertRaises(VKError):
            validate("store.update", {"market": "false"})

    def test_owner_mapping(self):
        self.assertEqual(api_params("product.get", {"item_id": 2}, 77), {"item_ids": "-77_2", "extended": 1})
        self.assertEqual(api_params("post.get", {"post_id": 2}, 77), {"posts": "-77_2"})

    def test_duplicate_json_keys_rejected(self):
        with self.assertRaises(VKError):
            loads('{"price":1,"price":2}')

    def test_no_redirect(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.com"))

    def test_write_transport_failure_not_retried(self):
        seen = []
        def transport(*args, **kwargs):
            seen.append(args)
            raise Uncertain("Нет ответа")
        api = API(lambda: "TEST_SECRET", transport=transport)
        with self.assertRaises(Uncertain):
            api.call("wall.post", {"message": "Тест"}, mutation=True)
        self.assertEqual(len(seen), 1)

    def test_api_error_does_not_leak_secret(self):
        api = API(lambda: "TEST_SECRET", transport=lambda *a, **k: {"error": {"error_code": 5, "error_msg": "TEST_SECRET", "request_params": [{"key": "access_token", "value": "TEST_SECRET"}]}})
        with self.assertRaises(VKError) as ctx:
            api.call("wall.get")
        self.assertNotIn("TEST_SECRET", str(ctx.exception))

    def test_api_form_not_query_string(self):
        seen = []
        def transport(url, body, **kwargs):
            seen.append((url, body))
            return {"response": 1}
        api = API(lambda: "TEST_SECRET", transport=transport)
        api.call("wall.post", {"owner_id": -77, "message": "Текст"}, mutation=True)
        self.assertNotIn("TEST_SECRET", seen[0][0])
        self.assertEqual(parse_qs(seen[0][1].decode())["access_token"], ["TEST_SECRET"])

    def test_mcp_handshake_and_notifications(self):
        source = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n'
                             '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
                             '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
        out = io.StringIO()
        self.assertEqual(serve(Path("."), source, out), 0)
        rows = [loads(row) for row in out.getvalue().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["result"]["protocolVersion"], "2025-06-18")
        names = [tool["name"] for tool in rows[1]["result"]["tools"]]
        self.assertIn("vk_apply", names)
        self.assertNotIn("vk_login", names)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve() / "project"
        self.root.mkdir()
        self.tokenfile = Path(self.tmp.name).resolve() / "private/token.json"
        initialize(self.root, "test_shop", "123")
        self.env = patch.dict(os.environ, {"BAMBOO_VK_TOKEN_FILE": str(self.tokenfile), "BAMBOO_VK_ACCESS_TOKEN": ""})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_manual_import_private_no_token_in_result(self):
        result = auth.import_token(self.root, "TEST_SECRET")
        self.assertNotIn("TEST_SECRET", str(result))
        self.assertEqual(auth.token(self.root), "TEST_SECRET")
        if os.name == "posix":
            self.assertEqual(self.tokenfile.stat().st_mode & 0o777, 0o600)

    def test_secrets_inside_repository_forbidden(self):
        with patch.dict(os.environ, {"BAMBOO_VK_TOKEN_FILE": str(self.root / "secret.json")}):
            with self.assertRaises(VKError):
                auth.credential_path(self.root)

    def test_pkce_state_and_no_verifier_in_output(self):
        output = auth.start(self.root)
        pending = read(self.tokenfile.with_suffix(".pending.json"))
        self.assertNotIn(pending["verifier"], str(output))
        query = parse_qs(urlsplit(output["authorize_url"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        with self.assertRaises(VKError):
            auth.finish(self.root, "http://localhost/vk/callback?code=X&device_id=D&state=WRONG")

    def test_pkce_exchange_and_replay_rejected(self):
        auth.start(self.root)
        pending = read(self.tokenfile.with_suffix(".pending.json"))
        callback = "http://localhost/vk/callback?code=X&device_id=D&state=" + pending["state"]
        with patch("bamboo.vk.auth.request_json", return_value={"access_token": "TEST_SECRET", "refresh_token": "TEST_REFRESH", "expires_in": 3600, "state": pending["state"]}) as req:
            result = auth.finish(self.root, callback)
            self.assertTrue(result["saved"])
            self.assertNotIn("TEST_SECRET", str(result))
            self.assertEqual(req.call_args.args[1], b"code=X")
        with self.assertRaises(VKError):
            auth.finish(self.root, callback)

    def test_refresh_rotation(self):
        auth.save_tokens(self.tokenfile, {"access_token": "OLD", "refresh_token": "REFRESH_OLD", "expires_in": 1}, "D", "123", "http://localhost/vk/callback")
        def response(url, body):
            state = parse_qs(urlsplit(url).query)["state"][0]
            return {"access_token": "NEW", "refresh_token": "REFRESH_NEW", "expires_in": 3600, "state": state}
        with patch("bamboo.vk.auth.request_json", side_effect=response):
            self.assertEqual(auth.token(self.root), "NEW")
        self.assertEqual(read(self.tokenfile)["refresh_token"], "REFRESH_NEW")

    def test_callback_origin_mismatch(self):
        auth.start(self.root)
        with self.assertRaises(VKError):
            auth.finish(self.root, "https://attacker.example/?code=X&device_id=D&state=S")

    def test_key_file_permissions_rejected(self):
        if os.name != "posix":
            self.skipTest("POSIX mode check")
        auth.import_token(self.root, "TEST_SECRET")
        self.tokenfile.chmod(0o644)
        with self.assertRaises(VKError):
            auth.token(self.root)


if __name__ == "__main__":
    unittest.main()
