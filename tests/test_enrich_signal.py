"""enrich 必须发一个"结束"信号。

否则状态栏会永远挂着最后一条"列表可用 · 后台读取数据 N/M"——
这正是用户截图里那条不该在抓取结束后还出现的文字。
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from web_app import Api


def photo(ident):
    return {"id": str(ident), "url": f"https://www.tiktok.com/@owner/photo/{ident}",
            "type": "image"}


class EnrichSignalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()
        self.api._cookie_browser = ""
        self.api._cookie_file = ""
        self.calls = []
        self.api._emit = lambda function, value: self.calls.append((function, value))

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def statuses(self):
        return [value for function, value in self.calls if function == "metadataStatus"]

    def test_progress_is_followed_by_a_done_signal(self):
        self.api.enrich([photo(1000), photo(1001), photo(1002)])
        progress = self.statuses()[:-1]
        self.assertEqual([p["current"] for p in progress], [1, 2, 3])
        self.assertTrue(self.statuses()[-1].get("done"),
                        "enrich 结束必须发终点信号，否则状态栏会残留进度文字")

    def test_done_is_the_last_signal_not_the_first(self):
        self.api.enrich([photo(1000)])
        self.assertEqual(len(self.statuses()), 2)
        self.assertNotIn("done", self.statuses()[0])
        self.assertIn("done", self.statuses()[1])

    def test_a_failing_extraction_still_reports_done(self):
        videos = [{"id": "1", "url": "https://www.tiktok.com/@owner/video/1", "type": "video"}]
        with patch.object(self.api, "_youtube_dl", side_effect=RuntimeError("no network")):
            self.api.enrich(videos)
        self.assertTrue(self.statuses()[-1].get("done"),
                        "抽取失败也不能把进度文字留在状态栏上")

    def test_an_already_enriched_list_still_reports_done(self):
        # likes + upload_date 都有时直接 continue，不推进度 —— 但终点信号不能少
        videos = [{"id": "1", "url": "https://www.tiktok.com/@owner/video/1", "type": "video",
                   "likes": 5, "upload_date": "20260101"}]
        self.api.enrich(videos)
        self.assertEqual(self.statuses(), [{"done": True}])

    def test_an_empty_list_emits_nothing(self):
        self.assertEqual(self.api.enrich([]), {"updated": 0})
        self.assertEqual(self.statuses(), [])


if __name__ == "__main__":
    unittest.main()
