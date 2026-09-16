"""桥接门面（CollectorApi）的回归网：界面能调的采集接口必须真的能用。

这一层是给 Integration Agent 用的：content_bridge.py 一行转发即可接上，
所以这里把「转发目标」逐个测掉 —— 免得接上去才发现某个方法签名对不上。

顺带锁住一条容易踩的坑：ContentFactoryApi.__dir__ 只列自己类体里的名字，
所以**必须**一行一行显式转发，不能靠 mixin 继承（继承的方法 pywebview 发现不了）。
"""

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_factory.collector.bridge_api import CollectorApi  # noqa: E402
from content_factory.collector.sources import StaticCandidateSource  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402

# 桥接层需要一行一行转发的方法（名字就是对外契约）
EXPECTED_METHODS = (
    "content_creator_list", "content_creator_save", "content_creator_delete",
    "content_creator_toggle", "content_creator_set_interval",
    "content_creator_set_priority", "content_creator_check_now",
    "content_collect_tick", "content_collector_status", "content_collection_jobs",
    "content_collection_runs", "content_collection_failures", "content_collect_download",
    "content_collector_start", "content_collector_stop",
)

VIDEO_A = {"id": "7312000000000000001",
           "url": "https://www.tiktok.com/@emilyintech/video/7312000000000000001",
           "title": "3 habits", "duration": 40, "type": "video"}


def cleanup(temp, attempts=20):
    """删临时目录，容忍后台线程刚刚才释放文件句柄。"""
    for _ in range(attempts):
        try:
            temp.cleanup()
            return True
        except (PermissionError, OSError):
            time.sleep(0.5)
    return False


class FakeDownloader:
    def __init__(self):
        self.calls = []

    def download(self, videos, folder, quality, retry_count=3, concurrency=1):
        self.calls.append({"videos": videos, "folder": folder})
        return {"ok": len(videos), "failed": [], "folder": folder}


class CollectorApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.store = FactoryStore(self.root / "content-factory.db")
        self.settings = FactorySettings(self.root)
        self.settings.update("collect", {"keywords_block": []})
        self.settings.update("storage", {"video_path": str(self.root / "downloads")})
        self.events = []
        self.downloader = FakeDownloader()
        self.api = CollectorApi(self.store, settings=self.settings, downloader=self.downloader,
                                emit=lambda name, payload: self.events.append(name),
                                source=StaticCandidateSource(videos=[VIDEO_A], complete=True))

    def tearDown(self):
        try:
            self.api.content_collector_stop()
        except Exception:
            pass
        self.wait_idle()
        self.env.stop()
        cleanup(self.temp)

    def wait_idle(self, timeout=60):
        """等后台线程（立即检查 / 常驻轮询）真的收工，再删临时目录。

        Windows 上还有线程握着 sqlite 文件时删目录会报 WinError 32，
        这台机器一次提交要一秒级，所以必须等而不是拍脑袋 sleep。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            runner = self.api._runner
            checking = self.api.collector.monitor.stats()["checking"]
            if not checking and not (runner and runner.running):
                return True
            time.sleep(0.2)
        return False

    def test_every_method_the_bridge_will_forward_exists(self):
        for name in EXPECTED_METHODS:
            self.assertTrue(callable(getattr(self.api, name, None)), f"缺少桥接方法 {name}")

    def test_creator_crud_through_the_bridge_surface(self):
        created = self.api.content_creator_save({"handle": "emilyintech", "display_name": "Emily",
                                                 "priority": "高", "poll_interval": "30 分钟"})
        self.assertTrue(created["ok"], created)
        creator_id = created["creatorId"]
        self.assertEqual(len(created["creators"]), 1)

        updated = self.api.content_creator_save({"id": creator_id, "category": "AI 科技"})
        self.assertTrue(updated["ok"])
        self.assertEqual(updated["creator"]["category"], "AI 科技")

        duplicate = self.api.content_creator_save({"handle": "emilyintech"})
        self.assertFalse(duplicate["ok"])
        self.assertTrue(duplicate["duplicate"])

        listing = self.api.content_creator_list()
        self.assertEqual(listing["stats"]["total"], 1)
        self.assertEqual(listing["stats"]["due"], 1)

        toggled = self.api.content_creator_toggle(creator_id, False)
        self.assertFalse(toggled["creator"]["enabled"])
        self.assertEqual(self.api.content_creator_list(enabled=False)["stats"]["disabled"], 1)

        self.api.content_creator_set_interval(creator_id, "2 小时")
        self.assertEqual(self.api.content_creator_list()["creators"][0]["poll_interval_seconds"],
                         7200)
        self.api.content_creator_set_priority(creator_id, "低")
        self.assertEqual(self.api.content_creator_list()["creators"][0]["priority"], "低")

        removed = self.api.content_creator_delete(creator_id)
        self.assertTrue(removed["ok"])
        self.assertEqual(self.api.content_creator_list()["stats"]["total"], 0)

    def test_tick_queues_and_the_queue_is_visible(self):
        creator_id = self.api.content_creator_save({"handle": "emilyintech"})["creatorId"]
        tick = self.api.content_collect_tick()
        self.assertEqual(tick["checked"], 1)
        self.assertEqual(tick["queued"], 1)

        jobs = self.api.content_collection_jobs()
        self.assertEqual(len(jobs["jobs"]), 1)
        self.assertEqual(jobs["counts"]["pending"], 1)
        self.assertEqual(jobs["jobs"][0]["content_key"], "tiktok:7312000000000000001")

        runs = self.api.content_collection_runs()
        self.assertEqual(runs["runs"][0]["state"], "success")
        self.assertEqual(self.api.content_collection_failures()["runs"], [])

        status = self.api.content_collector_status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["jobs"]["pending"], 1)
        self.assertEqual(status["creators"]["total"], 1)
        self.assertFalse(status["runner"]["running"])

        # 立即检查是后台线程，等它落地（队列里已有的任务不会重复建）
        self.assertTrue(self.api.content_creator_check_now(creator_id)["started"])
        for _ in range(100):
            if self.api.content_collection_runs()["summary"]["total"] >= 2:
                break
            time.sleep(0.05)
        self.assertGreaterEqual(self.api.content_collection_runs()["summary"]["total"], 2)
        self.assertEqual(self.api.content_collection_jobs()["counts"]["pending"], 1)

    def test_pending_jobs_go_to_the_existing_downloader(self):
        self.api.content_creator_save({"handle": "emilyintech"})
        self.api.content_collect_tick()
        result = self.api.content_collect_download(background=False)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(self.downloader.calls[0]["videos"][0]["id"], "7312000000000000001")
        self.assertTrue(self.downloader.calls[0]["folder"].endswith("downloads"))
        self.assertEqual(self.api.content_collection_jobs()["counts"]["done"], 1)

    def test_download_with_nothing_queued_says_so(self):
        result = self.api.content_collect_download()
        self.assertTrue(result["ok"])
        self.assertFalse(result["started"])
        self.assertIn("没有待下载", result["message"])

    def test_runner_can_be_started_and_stopped_from_the_bridge(self):
        self.api.content_creator_save({"handle": "emilyintech"})
        started = self.api.content_collector_start(interval=5)
        self.assertTrue(started["running"])
        self.assertEqual(started["interval"], 5)
        self.assertTrue(self.api.content_collector_status()["runner"]["running"])
        stopped = self.api.content_collector_stop()
        self.assertFalse(stopped["running"])

    def test_events_reach_the_bridge_callback(self):
        self.api.content_creator_save({"handle": "emilyintech"})
        self.api.content_collect_tick()
        self.assertIn("collectProgress", self.events)
        self.assertIn("collectJob", self.events)

    def test_missing_downloader_is_reported_not_crashed(self):
        """没有下载器时不能假装采集成功，要给出明确原因并留下失败记录。"""
        api = CollectorApi(self.store, settings=self.settings, downloader=None)
        creator_id = api.content_creator_save({"handle": "emilyintech"})["creatorId"]
        result = api.content_creator_check_now(creator_id, background=False)
        self.assertFalse(result["ok"])
        self.assertIn("下载器", result["error"])
        self.assertEqual(api.content_collection_runs()["runs"][0]["state"], "failed")


if __name__ == "__main__":
    unittest.main()
