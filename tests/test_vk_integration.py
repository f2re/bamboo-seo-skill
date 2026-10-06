"""Интеграция с настоящими локальными модулями Bamboo; VK API остаётся подменённым."""
import json
import subprocess
import sys

from test_bamboo import Fixture, ROOT
from test_vk_store import FakeAPI
from bamboo.install import SYSTEMS, install
from bamboo.vk.common import VKError
from bamboo.vk.store import Store, initialize


class EditorialVKTests(Fixture):
    def store(self):
        initialize(self.root, "fixture-shop", "123")
        result = Store(self.root, FakeAPI())
        result.check()
        return result

    def test_approved_pack_photo_and_post_end_to_end(self):
        self.approved("vk", photo=True)
        store = self.store()
        plan = store.plan_post("example", "Опубликовать утверждённый тестовый пакет")
        actions = plan["plan"]["actions"]
        self.assertEqual([a["operation"] for a in actions], ["photo.wall", "post.create"])
        self.assertNotIn("[[fact]]", actions[-1]["params"]["message"])
        self.assertIn("Задайте вопрос мастеру.", actions[-1]["params"]["message"])
        self.assertEqual(actions[-1]["params"]["attachments"], ["$0.attachment"])
        self.assertTrue(store.apply(plan["plan_id"], plan["confirmation"], True)["ok"])
        self.assertEqual(len(store.api.writes), 2)

    def test_unapproved_pack_cannot_be_planned(self):
        self.good("vk", photo=True)
        store = self.store()
        with self.assertRaises(VKError):
            store.plan_post("example", "Попытка без утверждения")
        self.assertFalse(store.api.writes)

    def test_changed_pack_blocks_first_upload(self):
        self.approved("vk", photo=True)
        store = self.store()
        plan = store.plan_post("example", "Тестовое поручение")
        self.change("pack.json", lambda p: p["formats"]["vk"].update(text="Другая версия"))
        with self.assertRaises(VKError):
            store.apply(plan["plan_id"], plan["confirmation"], True)
        self.assertFalse(store.api.writes)

    def test_installed_vk_cli_and_mcp_offline(self):
        target = self.root / "installed"
        install(ROOT, target, SYSTEMS)
        command = [sys.executable, str(target / "bamboo.py"), "--workspace", str(target), "vk"]
        result = subprocess.run(command + ["operations"], capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("product.update", json.loads(result.stdout)["operations"])
        request = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n'
        request += '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
        result = subprocess.run(command + ["mcp"], input=request, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        responses = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertIn("vk_apply", [t["name"] for t in responses[1]["result"]["tools"]])
