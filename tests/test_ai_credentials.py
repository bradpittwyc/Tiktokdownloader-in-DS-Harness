"""AI 密钥的加密存储与迁移。

背景：`learning.json` 里**曾经明文存着 API Key**（审计时在本机实测确认）。
这一组测试盯三件事：

1. 明文必须被搬进 DPAPI，并且从设置文件里**消失**
2. 前端**永远拿不到明文** —— 只回答"配没配、怎么存的"
3. 搬不动的时候**不能把用户的 Key 弄丢** —— 宁可暂时还是明文，也不能让功能不可用

全部离线，用的是真实的 Windows DPAPI（换台机器就用不了，这是设计如此）。
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "outputs/TikTokBatchMVP"))
from ai_credentials import DpapiSecret  # noqa: E402
from web_app import Api  # noqa: E402

SECRET = "sk-live-MUST-NOT-LEAK-1234567890"


class DpapiSecretTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DpapiSecret("ai-key.dpapi", root=self.temp.name)

    def test_round_trip(self):
        self.assertFalse(self.store.has())
        self.assertIsNone(self.store.get())
        self.store.set(SECRET)
        self.assertTrue(self.store.has())
        self.assertEqual(self.store.get(), SECRET)

    def test_the_file_does_not_contain_the_plaintext(self):
        self.store.set(SECRET)
        blob = self.store.path.read_bytes()
        self.assertNotIn(SECRET.encode("utf-8"), blob)
        self.assertGreater(len(blob), len(SECRET), "加密后不该比明文还短")

    def test_remove(self):
        self.store.set(SECRET)
        self.assertTrue(self.store.remove())
        self.assertFalse(self.store.remove(), "再删一次应该返回 False")
        self.assertFalse(self.store.has())
        self.assertIsNone(self.store.get())

    def test_an_empty_value_is_rejected_not_silently_deleted(self):
        # "设置成空"和"清除"是两件事，不能让前者悄悄变成后者
        with self.assertRaises(ValueError):
            self.store.set("")
        with self.assertRaises(ValueError):
            self.store.set(None)

    def test_a_corrupted_file_reads_as_none_instead_of_raising(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_bytes(b"not encrypted at all")
        self.assertTrue(self.store.has())
        self.assertIsNone(self.store.get(), "解不开就返回 None，让上层提示重填")

    def test_an_empty_file_counts_as_not_configured(self):
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        self.store.path.write_bytes(b"")
        self.assertFalse(self.store.has())

    def test_two_stores_do_not_collide(self):
        other = DpapiSecret("github-token.dpapi", root=self.temp.name)
        self.store.set(SECRET)
        other.set("ghp_other")
        self.assertEqual(self.store.get(), SECRET)
        self.assertEqual(other.get(), "ghp_other")


class MigrationTests(unittest.TestCase):
    """旧的明文 Key 必须被搬走，而且搬不动时不能丢。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.learning_file = Path(self.temp.name) / "TikTokBatchMVP" / "learning.json"

    def write_learning(self, payload):
        self.learning_file.parent.mkdir(parents=True, exist_ok=True)
        self.learning_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def read_learning(self):
        return json.loads(self.learning_file.read_text(encoding="utf-8"))

    def test_a_plaintext_key_is_moved_into_dpapi_and_erased(self):
        self.write_learning({"provider": "deepseek", "api_key": SECRET})
        api = Api()
        self.assertEqual(self.read_learning()["api_key"], "", "明文必须从设置文件里消失")
        self.assertNotIn(SECRET, self.learning_file.read_text(encoding="utf-8"))
        self.assertTrue(api._credentials.has())
        self.assertEqual(api._api_key(), SECRET, "搬完还得能用")

    def test_a_key_already_in_dpapi_is_left_alone(self):
        self.write_learning({"provider": "deepseek", "api_key": ""})
        api = Api()
        api._credentials.set(SECRET)
        api2 = Api()
        self.assertEqual(api2._api_key(), SECRET)
        self.assertEqual(self.read_learning()["api_key"], "")

    def test_other_options_survive_the_migration(self):
        self.write_learning({"provider": "deepseek", "model": "deepseek-chat",
                             "enabled": True, "vocabulary": False, "api_key": SECRET})
        api = Api()
        saved = self.read_learning()
        self.assertEqual(saved["provider"], "deepseek")
        self.assertEqual(saved["model"], "deepseek-chat")
        self.assertTrue(saved["enabled"])
        self.assertFalse(saved["vocabulary"])
        self.assertEqual(api._api_key(), SECRET)

    def test_a_failed_migration_keeps_the_key_usable(self):
        # DPAPI 写不进去时，宁可暂时还是明文，也不能让用户的 Key 凭空消失
        self.write_learning({"provider": "deepseek", "api_key": SECRET})
        with patch.object(DpapiSecret, "set", side_effect=OSError("DPAPI 不可用")):
            api = Api()
        self.assertEqual(api._api_key(), SECRET, "迁移失败也必须还能用")
        self.assertEqual(self.read_learning()["api_key"], SECRET,
                         "迁移失败时不该把文件里的明文抹掉")

    def test_no_plaintext_no_migration(self):
        self.write_learning({"provider": "deepseek", "api_key": ""})
        api = Api()
        self.assertFalse(api._credentials.has())
        self.assertEqual(api._api_key(), "")


class KeyNeverLeavesTheBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.api._credentials.set(SECRET)

    def test_get_learning_options_never_returns_the_key(self):
        payload = self.api.get_learning_options()
        self.assertEqual(payload["api_key"], "")
        self.assertTrue(payload["apiKeySet"])
        self.assertTrue(payload["keyEncrypted"])
        self.assertNotIn(SECRET, json.dumps(payload, ensure_ascii=False))

    def test_saving_never_writes_the_key_to_the_settings_file(self):
        self.api.set_learning_options({**self.api.get_learning_options(),
                                       "api_key": "sk-brand-new-value"})
        raw = self.api._learning_file().read_text(encoding="utf-8")
        self.assertNotIn("sk-brand-new-value", raw)
        self.assertNotIn(SECRET, raw)
        self.assertEqual(json.loads(raw)["api_key"], "")
        self.assertEqual(self.api._api_key(), "sk-brand-new-value", "新值要进 DPAPI")

    def test_an_empty_key_means_unchanged_not_cleared(self):
        # 界面每次都把输入框清空，不能因此把已存的 Key 抹掉
        self.api.set_learning_options({**self.api.get_learning_options(), "api_key": ""})
        self.assertEqual(self.api._api_key(), SECRET)
        self.api.set_learning_options({**self.api.get_learning_options(), "model": "deepseek-chat"})
        self.assertEqual(self.api._api_key(), SECRET, "改别的字段不该影响密钥")

    def test_clearing_is_explicit(self):
        result = self.api.clear_learning_api_key()
        self.assertTrue(result["ok"])
        self.assertTrue(result["removed"])
        self.assertFalse(self.api.get_learning_options()["apiKeySet"])
        self.assertEqual(self.api._api_key(), "")
        self.assertFalse(self.api.clear_learning_api_key()["removed"], "再清一次没有东西可删")

    def test_the_model_call_uses_the_stored_key(self):
        captured = {}

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "OK"}}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers or {}
            return FakeResponse()

        with patch("web_app.requests.post", side_effect=fake_post):
            self.api._call_model([{"role": "user", "content": "hi"}])
        self.assertEqual(captured["headers"]["Authorization"], f"Bearer {SECRET}")
        self.assertIn("/chat/completions", captured["url"])

    def test_a_missing_key_fails_before_any_request(self):
        self.api.clear_learning_api_key()
        with patch("web_app.requests.post") as post:
            with self.assertRaises(RuntimeError):
                self.api._call_model([{"role": "user", "content": "hi"}])
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
