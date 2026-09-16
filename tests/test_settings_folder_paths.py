"""存储设置的本地路径必须真的保存下来（RC1 第三个试玩问题）。

现象：在「设置 → 存储设置」里用「浏览」选好视频保存路径，点「保存设置」，
界面看着正常，但落盘的 storage.video_path 还是空 —— 于是「立即跑 1 条」
一直提示「未配置下载目录」。

根因在界面层（见 tests/test_content_factory_ui.py 的同名回归测试）：
「浏览」按钮也带了 data-key，collectSettings 用 `[data-key]` 收集时把它当成
一个设置字段，button.value 是空串，紧跟在同名 input 后面把刚选好的路径覆盖掉。

这一份守的是**落盘与回读**这一段，是用户真正关心的结果：
- 四个本地路径（video / cover / library / temp）保存后要能原样读回
- settings JSON 真的写进磁盘（不是只在内存里改了一下）
- 重启（新的 FactorySettings / 新的桥接实例）后仍然是那条路径
- Collector 读到的下载目录就是保存的那条，前置检查不再报 needsFolder
- 其它分区的普通设置字段不回归
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_bridge import ContentFactoryApi  # noqa: E402
from content_factory.collector.bridge_api import CollectorApi  # noqa: E402
from content_factory.collector.sources import StaticCandidateSource  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402

SETTINGS_FILE = "content-factory-settings.json"
LOCAL_PATHS = {
    "video_path": r"E:\ContentFactory\Videos",
    "cover_path": r"E:\ContentFactory\Covers",
    "library_path": r"E:\ContentFactory\Library",
    "temp_path": r"E:\ContentFactory\Temp",
}


class ModernFolderSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.db = self.root / "content-factory.db"

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def bridge(self, folder=None, downloader=None):
        """一个桥接实例；folder 给定时先把下载目录写进设置。"""
        settings = FactorySettings(self.root)
        if folder is not None:
            settings.update("storage", {"video_path": str(folder)})
        return ContentFactoryApi(downloader=downloader, store=FactoryStore(self.db),
                                 settings=settings)

    def settings_path(self):
        """设置文件放在 FactorySettings 的 root 下（测试里 root 就是临时目录）。"""
        return self.root / SETTINGS_FILE

    def json_on_disk(self):
        path = self.settings_path()
        self.assertTrue(path.is_file(), f"设置文件没有落盘：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    # ---- 四个本地路径 --------------------------------------------------
    def test_all_four_local_paths_round_trip_through_the_bridge(self):
        api = self.bridge()
        result = api.content_save_settings("storage", dict(LOCAL_PATHS))
        self.assertTrue(result["ok"], result)

        view = api.content_settings()["storage"]
        for key, value in LOCAL_PATHS.items():
            self.assertEqual(view[key], value, f"{key} 没有正确回读")

    def test_local_paths_are_really_written_to_the_settings_file(self):
        """不能只是内存里改了一下：要能在磁盘上的 JSON 里看到。"""
        api = self.bridge()
        api.content_save_settings("storage", dict(LOCAL_PATHS))

        stored = self.json_on_disk()["storage"]
        for key, value in LOCAL_PATHS.items():
            self.assertEqual(stored[key], value, f"{key} 没有写进设置文件")

    def test_paths_survive_a_restart(self):
        """重启 = 新的 FactorySettings + 新的桥接实例，读同一个目录。"""
        self.bridge().content_save_settings("storage", dict(LOCAL_PATHS))

        restarted = self.bridge()
        view = restarted.content_settings()["storage"]
        for key, value in LOCAL_PATHS.items():
            self.assertEqual(view[key], value, f"重启后 {key} 丢了")
        # 直接问 Settings Core，不看界面那一层
        direct = FactorySettings(self.root).get("storage", "video_path")
        self.assertEqual(direct, LOCAL_PATHS["video_path"])

    def test_bootstrap_also_reports_the_saved_path(self):
        """界面启动时走的是 content_bootstrap，它也必须给出同样的路径。"""
        api = self.bridge()
        api.content_save_settings("storage", dict(LOCAL_PATHS))
        payload = self.bridge().content_bootstrap()
        self.assertEqual(payload["settings"]["storage"]["video_path"],
                         LOCAL_PATHS["video_path"])

    def test_an_empty_value_is_still_allowed_to_clear_a_path(self):
        """用户真想把路径清空时也要能清空（收紧收集器不等于禁止空值）。"""
        api = self.bridge(folder=LOCAL_PATHS["video_path"])
        api.content_save_settings("storage", {"video_path": ""})
        self.assertEqual(api.content_settings()["storage"]["video_path"], "")
        self.assertEqual(FactorySettings(self.root).get("storage", "video_path"), "")

    # ---- 与 Collector / 单条闭环的衔接 ---------------------------------
    def test_collector_reads_the_saved_download_folder(self):
        api = self.bridge()
        api.content_save_settings("storage", {"video_path": LOCAL_PATHS["video_path"]})
        collector = api._collector
        self.assertIsInstance(collector, CollectorApi)
        self.assertEqual(collector.collector.download_folder(), LOCAL_PATHS["video_path"])

    def test_run_one_no_longer_reports_needs_folder(self):
        api = self.bridge()
        api.content_save_settings("storage", {"video_path": LOCAL_PATHS["video_path"]})
        creator_id = api.content_creator_save({"handle": "nasa"})["creatorId"]

        result = api.content_run_one_creator(creator_id, background=False)

        self.assertFalse(result.get("needsFolder"), result)
        self.assertNotIn("下载目录", str(result.get("error") or ""))

    def test_run_one_still_reports_needs_folder_when_unset(self):
        """没配目录时仍然要明确拒绝 —— 这条不能因为这次修复被放松。"""
        api = self.bridge()
        creator_id = api.content_creator_save({"handle": "nasa"})["creatorId"]

        result = api.content_run_one_creator(creator_id, background=False)

        self.assertFalse(result["ok"])
        self.assertTrue(result["needsFolder"])
        self.assertIn("下载目录", result["error"])
        self.assertIn("存储设置", result["error"])

    def test_a_saved_folder_is_what_the_run_actually_uses(self):
        """保存的目录要真的被单条闭环用上（下载器收到的就是它）。"""
        folder = self.root / "chosen-downloads"
        folder.mkdir()
        calls = []

        class RecordingDownloader:
            def download(self, videos, target, quality, retry_count=3, concurrency=1):
                calls.append(str(target))
                return {"ok": len(videos), "failed": [], "folder": str(target)}

        api = self.bridge(downloader=RecordingDownloader())
        api.content_save_settings("storage", {"video_path": str(folder)})
        source = StaticCandidateSource(videos=[{
            "id": "7312000000000000001",
            "url": "https://www.tiktok.com/@nasa/video/7312000000000000001",
            "title": "clip", "duration": 30, "type": "video"}], complete=True)
        api._collector.collector.source = source
        creator_id = api.content_creator_save({"handle": "nasa"})["creatorId"]

        result = api.content_run_one_creator(creator_id, background=False)

        self.assertEqual(calls, [str(folder)], "下载器应该收到刚保存的那个目录")
        # 假下载器不回调入库，所以这里停在 library 这一层 —— 但目录已经证明被用上了
        self.assertEqual(result["stage"], "library", result)


class OtherSectionsStillSaveTests(unittest.TestCase):
    """收紧 collectSettings 之后，其它分区的普通字段不能漏掉。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.api = ContentFactoryApi(store=FactoryStore(self.root / "cf.db"),
                                     settings=FactorySettings(self.root))

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_each_section_still_round_trips_its_own_fields(self):
        cases = {
            "general": {"theme": "light", "auto_start": False},
            "collect": {"interval_minutes": 45, "auto_collect": False,
                        "keywords_block": ["ad", "promo"], "download_quality": "720p"},
            "ai": {"model": "deepseek-reasoner", "max_tokens": 8000, "temperature": 0.3,
                   "detect_grammar": False, "prompt_template": "标题：{title}\n字幕：{transcript}"},
            "publish": {"daily_count": 7, "review_mode": "manual",
                        "targets": ["抖音", "B站"]},
            "notify": {"smtp_port": 2525, "channel_feishu": True, "quiet_start": "22:30"},
            "storage": {"bucket": "my-bucket", "keep_video_days": 30, "resume_upload": False},
            "account": {"display_name": "Tony C", "two_factor": False},
        }
        for section, values in cases.items():
            with self.subTest(section=section):
                result = self.api.content_save_settings(section, values)
                self.assertTrue(result["ok"], result)
                view = self.api.content_settings()[section]
                for key, value in values.items():
                    self.assertEqual(view[key], value, f"{section}.{key} 没保存上")

    def test_secret_fields_still_keep_their_previous_value_when_blank(self):
        """密码类字段留空 = 不变，这个语义不能被这次改动带偏。"""
        self.api.content_save_settings("ai", {"api_key": "sk-test-key"})
        self.assertTrue(self.api.content_settings()["ai"]["apiKeySet"])

        self.api.content_save_settings("ai", {"api_key": ""})       # 界面留空 = 不改
        self.assertTrue(self.api.content_settings()["ai"]["apiKeySet"],
                        "留空不应该把已保存的 Key 清掉")
        self.assertEqual(FactorySettings(self.root).section("ai")["api_key"], "sk-test-key")


if __name__ == "__main__":
    unittest.main()
