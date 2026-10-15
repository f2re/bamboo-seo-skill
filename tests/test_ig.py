"""Instagram-модуль: без сети, реальных ключей и аккаунтов."""
from __future__ import annotations

import calendar
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from bamboo.ig import secrets
from bamboo.ig.errors import IGError, Rejected, Uncertain
from bamboo.ig.graph import Graph, request_json
from bamboo.ig.publisher import Publisher, check_caption, initialize
from bamboo.ig.storage import Storage

MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"\x00" * 64
CAPTION = "Куринуки: чашу высекают из цельного куска глины.\n\n#куринуки #керамика"


class FakeGraph:
    def __init__(self, statuses=("FINISHED",)):
        self.statuses, self.calls, self.published = list(statuses), [], {}
        self.publish_error = None

    def me(self):
        self.calls.append("me")
        return {"user_id": "1784", "username": "bamboopottery"}

    def create_reel(self, user_id, url, caption, share_to_feed=True):
        self.calls.append(("create", url, caption))
        return "900"

    def wait(self, container_id, timeout=300, every=15):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        self.calls.append(("wait", status))
        return status

    def publish(self, user_id, container_id):
        self.calls.append(("publish", container_id))
        if self.publish_error:
            error, self.publish_error = self.publish_error, None
            self.published[container_id] = "777"
            self.statuses = ["PUBLISHED"]
            raise error
        self.published[container_id] = "777"
        return "777"

    def recent(self, user_id):
        return [{"id": "777", "caption": CAPTION}]

    def media(self, media_id):
        return {"id": media_id, "permalink": "https://www.instagram.com/reel/TEST/", "media_product_type": "REELS"}


class FakeStorage:
    def __init__(self):
        self.objects, self.deleted = {}, []

    def put(self, key, body, content_type="video/mp4"):
        self.objects[key] = body

    def presign_get(self, key, expires=3600):
        return f"https://storage.yandexcloud.net/bucket/{key}?X-Amz-Signature=test"

    def exists(self, key):
        return key in self.objects

    def delete(self, key):
        self.deleted.append(key)


class StorageTests(unittest.TestCase):
    def test_presign_matches_aws_reference_vector(self):
        t = calendar.timegm((2013, 5, 24, 0, 0, 0))
        s = Storage("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "examplebucket",
                    region="us-east-1", clock=lambda: t)
        url = s.presign_get("test.txt", 86400, host="examplebucket.s3.amazonaws.com", path="/test.txt")
        self.assertTrue(url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"))

    def test_put_is_signed_and_secret_not_sent(self):
        sent = []

        class Response(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(request, timeout=60):
            sent.append(request)
            return Response(b"")
        s = Storage("KEYID", "TOPSECRET", "bamboo", opener=opener)
        s.put("bamboo-ig/x.mp4", MP4)
        request = sent[0]
        self.assertEqual(request.full_url, "https://storage.yandexcloud.net/bamboo/bamboo-ig/x.mp4")
        auth = request.get_header("Authorization")
        self.assertIn("Credential=KEYID/", auth)
        self.assertIn("/ru-central1/s3/aws4_request", auth)
        self.assertNotIn("TOPSECRET", auth)
        self.assertEqual(request.get_header("X-amz-content-sha256"), hashlib.sha256(MP4).hexdigest())

    def test_rejects_bad_bucket_and_key(self):
        for bucket in ("a.b", "a/b", ""):
            with self.assertRaises(IGError):
                Storage("K", "S", bucket)
        with self.assertRaises(IGError):
            Storage("K", "S", "bamboo").path("../x")

    def test_forbidden_is_clear_error(self):
        def opener(request, timeout=60):
            raise HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b""))
        with self.assertRaises(IGError) as caught:
            Storage("K", "S", "bamboo", opener=opener).exists("x")
        self.assertIn("storage.editor", str(caught.exception))


class GraphTests(unittest.TestCase):
    def test_only_graph_host(self):
        with self.assertRaises(IGError):
            request_json("GET", "https://evil.example/v25.0/me")

    def test_error_mapped_without_server_text(self):
        def opener(request, timeout=60):
            raise HTTPError(request.full_url, 400, "Bad", {}, io.BytesIO(
                json.dumps({"error": {"code": 190, "message": "token SECRETVALUE"}}).encode()))
        with self.assertRaises(Rejected) as caught:
            Graph("SECRETVALUE", opener=opener).me()
        self.assertIn("ig token-import", str(caught.exception))
        self.assertNotIn("SECRETVALUE", str(caught.exception))

    def test_lost_write_is_uncertain(self):
        def opener(request, timeout=60):
            raise OSError("reset")
        with self.assertRaises(Uncertain):
            Graph("T", opener=opener).publish("1", "2")

    def test_wait_polls_until_finished(self):
        g = Graph("T", sleep=lambda s: None)
        with patch.object(Graph, "status", side_effect=["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]):
            self.assertEqual(g.wait("1"), "FINISHED")

    def test_caption_limits(self):
        check_caption(CAPTION)
        with self.assertRaises(IGError):
            check_caption(" ".join(f"#t{i}" for i in range(6)))
        with self.assertRaises(IGError):
            check_caption("а" * 2201)


class SecretsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name).resolve()
        self.root = base / "project"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"BAMBOO_IG_SECRETS_FILE": str(base / "home/ig.json")})
        self.env.start()
        os.environ.pop("BAMBOO_IG_ACCESS_TOKEN", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_import_rejects_urls_and_params(self):
        for bad in ("https://x/?access_token=1", "access_token=1&x=2", "two words", ""):
            with self.assertRaises(IGError):
                secrets.import_token(self.root, bad)

    def test_file_inside_project_rejected(self):
        with patch.dict(os.environ, {"BAMBOO_IG_SECRETS_FILE": str(self.root / "ig.json")}):
            with self.assertRaises(IGError):
                secrets.import_token(self.root, "IGTEST")

    def test_refresh_old_token_near_expiry(self):
        secrets.import_token(self.root, "OLD")
        path = secrets.secrets_path(self.root)
        data = json.loads(path.read_text())
        data.update(obtained_at=time.time() - 40 * 86400, expires_at=time.time() + 20 * 86400)
        path.write_text(json.dumps(data))
        with patch("bamboo.ig.secrets.refresh", return_value={"access_token": "NEW", "expires_in": 5184000}) as refresh:
            self.assertEqual(secrets.token(self.root), "NEW")
        refresh.assert_called_once()
        self.assertEqual(secrets.token(self.root), "NEW")

    def test_fresh_token_not_refreshed(self):
        secrets.import_token(self.root, "FRESH")
        with patch("bamboo.ig.secrets.refresh") as refresh:
            self.assertEqual(secrets.token(self.root), "FRESH")
        refresh.assert_not_called()


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "content/media").mkdir(parents=True)
        (self.root / "content/media/reel.mp4").write_bytes(MP4)
        initialize(self.root, "bamboo")
        self.approved = patch("bamboo.ig.publisher.approved_reel", side_effect=lambda root, job: (
            CAPTION, "content/media/reel.mp4", hashlib.sha256((root / "content/media/reel.mp4").read_bytes()).hexdigest(),
            "job@hash"))
        self.approved.start()

    def tearDown(self):
        self.approved.stop()
        self.tmp.cleanup()

    def make(self, graph=None):
        self.graph, self.storage = graph or FakeGraph(), FakeStorage()
        return Publisher(self.root, self.graph, self.storage)

    def plan(self, pub):
        return pub.plan_post("job", "Публикуй везде")

    def test_plan_is_offline_and_apply_publishes(self):
        pub = self.make()
        p = self.plan(pub)
        self.assertEqual(p["caption"], CAPTION)
        self.assertFalse(self.graph.calls)
        self.assertEqual(pub.apply(p["plan_id"])["network_called"], False)
        result = pub.apply(p["plan_id"], p["confirmation"], True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["permalink"], "https://www.instagram.com/reel/TEST/")
        self.assertEqual(self.storage.deleted, list(self.storage.objects))
        again = pub.apply(p["plan_id"], p["confirmation"], True)
        self.assertTrue(again["already_published"])
        self.assertEqual(sum(1 for c in self.graph.calls if c[0] == "publish"), 1)

    def test_wrong_confirmation_no_network(self):
        pub = self.make()
        p = self.plan(pub)
        with self.assertRaises(IGError):
            pub.apply(p["plan_id"], "ig:other", True)
        self.assertFalse(self.graph.calls)

    def test_processing_resumes_same_container(self):
        pub = self.make(FakeGraph(["IN_PROGRESS", "FINISHED"]))
        p = self.plan(pub)
        first = pub.apply(p["plan_id"], p["confirmation"], True)
        self.assertEqual(first["stage"], "processing")
        self.assertTrue(pub.apply(p["plan_id"], p["confirmation"], True)["ok"])
        self.assertEqual(sum(1 for c in self.graph.calls if c[0] == "create"), 1)

    def test_uncertain_publish_is_not_duplicated(self):
        graph = FakeGraph()
        graph.publish_error = Uncertain("lost")
        pub = self.make(graph)
        p = self.plan(pub)
        self.assertEqual(pub.apply(p["plan_id"], p["confirmation"], True)["stage"], "uncertain")
        result = pub.apply(p["plan_id"], p["confirmation"], True)
        self.assertTrue(result["ok"])
        self.assertEqual(sum(1 for c in graph.calls if c[0] == "publish"), 1)

    def test_container_error_allows_new_container(self):
        pub = self.make(FakeGraph(["ERROR", "FINISHED"]))
        p = self.plan(pub)
        with self.assertRaises(Rejected):
            pub.apply(p["plan_id"], p["confirmation"], True)
        self.assertTrue(pub.apply(p["plan_id"], p["confirmation"], True)["ok"])
        self.assertEqual(sum(1 for c in self.graph.calls if c[0] == "create"), 2)

    def test_video_changed_after_plan_nothing_uploaded(self):
        pub = self.make()
        p = self.plan(pub)
        (self.root / "content/media/reel.mp4").write_bytes(MP4 + b"x")
        with self.assertRaises(IGError):
            pub.apply(p["plan_id"], p["confirmation"], True)
        self.assertFalse(self.storage.objects)
        self.assertFalse([c for c in self.graph.calls if c[0] in ("create", "publish")])

    def test_tampered_plan_rejected(self):
        pub = self.make()
        p = self.plan(pub)
        path = self.root / f".bamboo/ig/plans/{p['plan_id']}.json"
        doc = json.loads(path.read_text())
        doc["payload"]["caption"] = "Другая подпись"
        path.write_text(json.dumps(doc))
        with self.assertRaises(IGError):
            pub.show(p["plan_id"])


if __name__ == "__main__":
    unittest.main()
