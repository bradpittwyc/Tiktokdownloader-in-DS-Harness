"""内容工厂桥接层：界面能调到什么、调不到什么。

这里的断言不是形式主义 —— pywebview 暴露 js_api 的机制是 dir() 递归 + getattr 链，
所以「桥接对象上到底有哪些可调用方法」是个会真实影响运行时的接口面：
- 漏暴露 → 界面上 window.pywebview.api.content_factory.content_xxx 直接 undefined
- 多暴露 → 内部服务方法变成对外契约，还会多出 content_factory.value.xxx 这类暗道

所以这里用与 pywebview util.py:180-211 完全相同的遍历规则复刻一遍，
把它当成契约测试锁住。
"""

import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_bridge import ContentFactoryApi, _Hidden, unwrap  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402

# 界面依赖的方法清单：少一个 UI 就有一个按钮点不动
EXPECTED = {
    "content_bootstrap", "content_stats", "content_items", "content_item",
    "content_creators", "content_errors", "content_topic_distribution",
    "content_settings", "content_save_settings", "content_reset_settings",
    "content_test_ai", "content_choose_folder", "content_open_folder",
    "content_enrich", "content_reanalyze", "content_enrich_pending",
    "content_set_transcript", "content_transcript",
    "content_ingest_videos", "content_import_folder", "content_register_download",
    "content_ingest_downloader", "content_seed_demo", "content_clear_demo",
    "content_worker_status", "content_events",
}


def exposed_functions(obj):
    """复刻 pywebview util.inject_pywebview 内部的 get_functions 遍历规则。"""
    seen, out = [], {}

    def walk(node, base=""):
        if id(node) in seen:
            return
        seen.append(id(node))
        for name in dir(node):
            if name.startswith("_"):
                continue
            try:
                attr = getattr(node, name)
            except Exception:
                continue
            if not getattr(attr, "_serializable", True):
                continue
            full = f"{base}.{name}" if base else name
            if inspect.ismethod(attr) or inspect.isfunction(attr):
                out[full] = list(inspect.signature(attr).parameters)[1:]
            elif inspect.isclass(attr) or (
                    isinstance(attr, object) and not callable(attr)
                    and hasattr(attr, "__module__")):
                walk(attr, full)

    walk(obj)
    return out


class ExposedApiSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = patch.dict(os.environ, {"LOCALAPPDATA": cls.temp.name})
        cls.env.start()
        cls.api = ContentFactoryApi(downloader=None)

    @classmethod
    def tearDownClass(cls):
        cls.env.stop()
        cls.temp.cleanup()

    def wrapped(self):
        """把桥接对象套一层 _Hidden，模拟 web_app.Api.content_factory 的真实形态。"""
        return _Hidden(self.api)

    def test_every_method_the_ui_calls_is_exposed(self):
        names = {name.split(".")[-1] for name in exposed_functions(self.wrapped())}
        self.assertTrue(EXPECTED.issubset(names),
                        f"界面要用但没暴露：{sorted(EXPECTED - names)}")

    def test_internal_service_methods_are_not_exposed(self):
        names = set(exposed_functions(self.wrapped()))
        for leaked in ("content_factory.value.content_stats",
                       "content_factory._store", "content_factory.store.upsert_item",
                       "content_factory.bridge_pipeline", "content_factory.downloader"):
            self.assertNotIn(leaked, names)

    def test_wrapper_refuses_anything_but_content_methods(self):
        wrapped = self.wrapped()
        # 只放行 content_*：这些名字一个都不能转发出去。
        # 注意 `_target` 不在此列 —— 它是包装对象自己的槽位，且下划线开头，
        # pywebview 会直接跳过（测试内部仍会用 unwrap 取真实对象）。
        for name in ("value", "store", "settings", "pipeline", "bridge_store",
                     "bridge_pipeline", "upsert_item", "public", "enrich_one"):
            with self.assertRaises(AttributeError, msg=f"{name} 不该被转发"):
                getattr(wrapped, name)
        self.assertTrue(callable(wrapped.content_stats))
        self.assertTrue(callable(wrapped.content_enrich))

    def test_unwrap_returns_the_real_object(self):
        wrapped = self.wrapped()
        self.assertIs(unwrap(wrapped), self.api)
        self.assertIs(unwrap(self.api), self.api)


class BridgeBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.store = FactoryStore(self.root / "cf.db")
        self.settings = FactorySettings(self.root)
        self.api = ContentFactoryApi(store=self.store, settings=self.settings)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_bootstrap_shape_matches_what_the_ui_reads(self):
        payload = self.api.content_bootstrap()
        for key in ("ok", "stats", "items", "creators", "settings", "errors", "stages"):
            self.assertIn(key, payload)
        self.assertEqual(len(payload["stages"]), 7, "流水线固定 7 个环节")
        self.assertIn("counts", payload["stats"])

    def test_settings_round_trip_through_the_bridge(self):
        result = self.api.content_save_settings("storage", {"video_path": "D:/TikTok/videos"})
        self.assertTrue(result["ok"])
        self.assertEqual(self.api.content_settings()["storage"]["video_path"], "D:/TikTok/videos")

    def test_unknown_settings_section_is_rejected(self):
        result = self.api.content_save_settings("nope", {"a": 1})
        self.assertFalse(result["ok"])
        self.assertIn("未知的设置分区", result["error"])

    def test_api_key_is_never_returned(self):
        self.api.content_save_settings("ai", {"api_key": "sk-secret"})
        self.assertEqual(self.api.content_settings()["ai"]["api_key"], "")
        self.assertTrue(self.api.content_settings()["ai"]["apiKeySet"])

    def test_reset_restores_defaults_for_one_section_only(self):
        self.api.content_save_settings("ai", {"model": "custom-model"})
        self.api.content_save_settings("storage", {"bucket": "my-bucket"})
        self.api.content_reset_settings("ai")
        settings = self.api.content_settings()
        self.assertNotEqual(settings["ai"]["model"], "custom-model")
        self.assertEqual(settings["storage"]["bucket"], "my-bucket", "恢复默认不能误伤别的分区")

    def test_register_download_then_read_it_back(self):
        """下载完成 → 内容库，这是「下载 → AI 标注」中间那一步。"""
        result = self.api.content_register_download({
            "id": "7312000000000000001", "title": "3 habits", "url": "https://www.tiktok.com/@emily/video/1",
            "folder": "D:/out", "media": "D:/out/clip.mp4",
            "subtitles": ["D:/out/clip.en.srt"], "state": "done",
        })
        self.assertTrue(result["ok"])
        self.assertTrue(result["hasSubtitle"])
        item = self.api.content_item(result["id"])["item"]
        self.assertEqual(item["download_status"], "done")
        self.assertEqual(item["creator_handle"], "emily")
        self.assertEqual(item["local_subtitle_path"], "D:/out/clip.en.srt")
        self.assertEqual(self.api.content_stats()["counts"]["downloaded"], 1)

    def test_enrich_without_api_key_reports_why(self):
        item_id = self.api.content_ingest_videos([{"id": "v1", "title": "T"}])["ids"][0]
        result = self.api.content_enrich(item_id, background=False)
        self.assertFalse(result["ok"])
        self.assertTrue(result["needsApiKey"])
        self.assertIn("API Key", self.api.content_item(item_id)["item"]["last_error"])

    def test_reanalyze_resets_failed_item_to_pending(self):
        item_id = self.api.content_ingest_videos([{"id": "v2", "title": "T"}])["ids"][0]
        self.store.set_ai_status(item_id, "failed", "旧错误")
        self.store.update_item(item_id, transcript_text="hello")
        result = self.api.content_reanalyze(item_id)
        self.assertTrue(result["ok"])
        # 后台线程会立刻把它标成 failed（未配 Key），但错误信息必须是新的
        for _ in range(60):
            row = self.api.content_item(item_id)["item"]
            if row["ai_status"] == "failed" and "API Key" in row["last_error"]:
                break
            import time
            time.sleep(0.05)
        self.assertIn("API Key", self.api.content_item(item_id)["item"]["last_error"])

    def test_demo_seed_and_clear_via_bridge(self):
        self.assertGreaterEqual(self.api.content_seed_demo()["created"], 10)
        self.assertGreater(self.api.content_stats()["counts"]["total"], 0)
        self.assertGreater(self.api.content_clear_demo()["removed"], 0)
        self.assertEqual(self.api.content_stats()["counts"]["total"], 0)

    def test_errors_page_reads_local_log_and_store(self):
        log = self.root / "TikTokBatchMVP" / "logs"
        log.mkdir(parents=True)
        (log / "scrape.log").write_text(
            "2026-01-15 14:28:32 下载失败: 请求超时\n", encoding="utf-8")
        item_id = self.store.upsert_item("e1", title="坏掉的作品")
        self.store.set_ai_status(item_id, "failed", "网络请求超时（Connection Timeout）")
        result = self.api.content_errors()
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["fromStore"], 1)
        self.assertTrue(result["logExists"])

    def test_worker_status_is_real_process_data(self):
        status = self.api.content_worker_status()
        self.assertTrue(status["online"])
        self.assertEqual(status["pid"], os.getpid())
        self.assertIn("uptimeText", status)

    def test_missing_folder_import_returns_a_clear_error(self):
        result = self.api.content_import_folder("Z:/definitely/not/here")
        self.assertFalse(result["ok"])
        self.assertIn("目录不存在", result["error"])

    def test_import_legacy_ai_credentials(self):
        """复用下载器学习文档里已配好的 Key，省得为 AI 标注再填一遍。"""
        legacy = self.root / "TikTokBatchMVP" / "learning.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({
            "api_key": "sk-legacy-key", "api_base": "https://api.deepseek.com/v1/",
            "model": "deepseek-chat", "enabled": True,
        }, ensure_ascii=False), encoding="utf-8")

        self.assertFalse(self.api.content_settings()["ai"]["apiKeySet"])
        result = self.api.content_import_legacy_ai()
        self.assertTrue(result["ok"], result)
        view = self.api.content_settings()["ai"]
        self.assertTrue(view["apiKeySet"], "导入后应显示已配置 Key")
        self.assertEqual(view["api_key"], "", "导入后界面依然拿不到明文")
        self.assertEqual(view["api_base"], "https://api.deepseek.com/v1", "末尾斜杠要去掉")
        self.assertEqual(view["model"], "deepseek-chat")
        # 真的落盘了：新建 settings 实例仍能读到
        self.assertEqual(FactorySettings(self.root).section("ai")["api_key"], "sk-legacy-key")

    def test_import_legacy_ai_without_file_is_reported(self):
        result = self.api.content_import_legacy_ai()
        self.assertFalse(result["ok"])
        self.assertIn("learning.json", result["error"])

    def test_import_legacy_ai_without_key_is_reported(self):
        legacy = self.root / "TikTokBatchMVP" / "learning.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"api_base": "https://x.test/v1"}), encoding="utf-8")
        result = self.api.content_import_legacy_ai()
        self.assertFalse(result["ok"])
        self.assertIn("没有 API Key", result["error"])


if __name__ == "__main__":
    unittest.main()
