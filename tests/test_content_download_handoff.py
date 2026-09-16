"""下载完成 → 内容工厂的交接（闭环里唯一还夹在下载器内部的环节）。

这条链路上有三段容易断的地方，各自都出过或差点出事：
1. `Api.download` 里那句 `_notify_content_factory(...)` 一旦被删/改错名字，
   下载照样成功、界面照样显示"已下载"，但内容库永远是空的，AI 加工页一无所获 ——
   而且没有任何报错。所以这里直接断言"下完一条，内容库里就多一条"。
2. 交接失败**不能影响下载主流程**：内容工厂自己的异常必须被吞掉并记日志。
3. 老用户升级上来时，本机已经下好的作品要能一键扫进内容库
   （content_ingest_downloader 读的是 localStorage 里的下载记录）。

全部离线：yt-dlp / 网络都被替换成假的。
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from web_app import Api  # noqa: E402


class FakeYdl:
    """最小可用的 yt-dlp 替身：按模板写一个"视频文件"和一个字幕文件。"""

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def download(self, urls):
        template = self.options["outtmpl"].replace(".%(ext)s", "")
        base = template.replace("%(title).150B", "clip").replace("%(upload_date)s", "20260115")
        base = base.replace("%(title)s", "clip")
        Path(base + ".mp4").write_bytes(b"fake video bytes")
        Path(base + ".en.srt").write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nAI is changing how we think about work.\n",
            encoding="utf-8")
        hook = (self.options.get("progress_hooks") or [None])[0]
        if hook:
            hook({"status": "finished", "filename": base + ".mp4"})


class DownloadHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.api = Api()
        self.folder = self.root / "downloads"
        self.folder.mkdir()
        self.item = {
            "id": "7312000000000000001",
            "title": "The real reason AI will change everything",
            "url": "https://www.tiktok.com/@techwithtim/video/7312000000000000001",
            "description": "demo",
            "duration": 72,
            "cover": "",
            "type": "video",
        }

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_download(self):
        with patch("web_app.Api._youtube_dl", lambda self, options: FakeYdl(options)), \
                patch("web_app.Api._photo_urls", lambda self, item: []):
            return self.api.download([self.item], str(self.folder), "1080p", retry_count=0)

    def library(self):
        return self.api.content_factory.content_items(status="all", limit=50)["items"]

    def test_a_finished_download_lands_in_the_content_library(self):
        result = self.run_download()
        self.assertEqual(result["ok"], 1, result)

        rows = self.library()
        self.assertEqual(len(rows), 1, "下载完成后内容库必须有这条内容")
        row = rows[0]
        self.assertEqual(row["source_video_id"], "7312000000000000001")
        self.assertEqual(row["creator_handle"], "techwithtim")
        self.assertEqual(row["download_status"], "done")
        self.assertTrue(row["local_video_path"].endswith(".mp4"), row["local_video_path"])
        self.assertTrue(row["local_subtitle_path"].endswith(".en.srt"), row["local_subtitle_path"])
        self.assertEqual(row["ai_status"], "pending", "刚下载的内容等待 AI 加工")

    def test_the_transcript_can_be_read_right_after_download(self):
        """下载 → 转写这一步必须是通的：字幕文件已经落盘，管道能读出文本。"""
        self.run_download()
        item_id = self.library()[0]["id"]
        result = self.api.content_factory.content_set_transcript(
            item_id, Path(self.library()[0]["local_subtitle_path"]).read_text(encoding="utf-8"))
        self.assertTrue(result["ok"])
        transcript = self.api.content_factory.content_transcript(item_id)
        self.assertIn("AI is changing how we think about work", transcript["text"])
        self.assertEqual(transcript["status"], "done")

    def test_re_downloading_the_same_video_does_not_duplicate_rows(self):
        self.run_download()
        self.run_download()
        self.assertEqual(len(self.library()), 1)

    def test_handoff_failure_does_not_break_the_download(self):
        """内容工厂抛异常时，下载必须照常返回成功。"""
        def boom(*_args, **_kwargs):
            raise RuntimeError("模拟内容工厂故障")

        with patch("web_app.Api.content_factory", property(boom)), \
                patch("web_app.Api._youtube_dl", lambda self, options: FakeYdl(options)), \
                patch("web_app.Api._photo_urls", lambda self, item: []):
            result = self.api.download([self.item], str(self.folder), "1080p", retry_count=0)
        self.assertEqual(result["ok"], 1, "内容工厂坏了不能连累下载")
        self.assertTrue(any(self.folder.rglob("*.mp4")), "文件仍要落盘")

    def test_ingest_existing_downloader_records(self):
        """老用户兜底路径：把 localStorage 里的下载记录扫进内容库。"""
        self.api._ui_records = {
            "emilyintech": {
                "7312000000000000002": {
                    "item": {"title": "3 habits", "url": "https://www.tiktok.com/@emilyintech/video/2",
                             "cover": "", "duration": 37, "description": ""},
                    "state": "done", "folder": str(self.folder), "subtitles": [],
                }
            }
        }
        result = self.api.content_factory.content_ingest_downloader()
        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 1)
        row = self.library()[0]
        self.assertEqual(row["creator_handle"], "emilyintech")
        self.assertEqual(row["title"], "3 habits")

    def test_ingest_without_records_reports_cleanly(self):
        result = self.api.content_factory.content_ingest_downloader()
        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 0)
        self.assertIn("还没有下载记录", result["message"])

    def test_items_survive_a_fresh_api_instance(self):
        """应用重启后记录仍在（新建 Api 实例 = 重新读同一个 sqlite）。"""
        self.run_download()
        before = self.library()[0]["id"]
        restarted = Api()
        rows = restarted.content_factory.content_items(status="all", limit=50)["items"]
        self.assertEqual([row["id"] for row in rows], [before])

    def test_progress_events_reach_the_bridge(self):
        """界面靠 enrichProgress 事件判断"分析到哪一步了"，事件必须真的发出来。

        重新分析是**后台线程**跑的（界面不能被网络请求卡住），所以这里要等一等，
        直接断言会偶发失败 —— 那种偶发正是最消耗排查时间的。
        """
        self.run_download()
        item_id = self.library()[0]["id"]
        self.api.content_factory.content_set_transcript(item_id, "some transcript text")
        result = self.api.content_factory.content_reanalyze(item_id)
        self.assertTrue(result["ok"])
        for _ in range(100):
            events = self.api.content_factory.content_events(since=0)["events"]
            if any(entry["name"] == "enrichProgress" for entry in events):
                break
            time.sleep(0.05)
        names = {entry["name"] for entry in self.api.content_factory.content_events(since=0)["events"]}
        self.assertIn("enrichProgress", names, "重新分析至少要回一个进度事件")
        # 没配 API Key 时应当明确落到失败态并说明原因，而不是静默停住
        for _ in range(100):
            if self.api.content_factory.content_item(item_id)["item"]["ai_status"] == "failed":
                break
            time.sleep(0.05)
        row = self.api.content_factory.content_item(item_id)["item"]
        self.assertEqual(row["ai_status"], "failed")
        self.assertIn("API Key", row["last_error"])


if __name__ == "__main__":
    unittest.main()
