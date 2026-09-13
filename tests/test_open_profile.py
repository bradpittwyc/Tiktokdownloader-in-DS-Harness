"""打开本地记录：已有归档时直接进页面，不再重新抓取。

关键不变量：
  1. open_profile 全程不碰浏览器（_collect_videos 一旦被调用就失败）
  2. 没有本地记录时必须回落去真抓，不能卡住
  3. 归档没抓完也要能打开，但要如实告诉用户
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
from web_app import Api


def video(ident):
    return {"id": str(ident), "url": f"https://www.tiktok.com/@owner/video/{ident}",
            "title": f"作品 {ident}", "type": "video"}


class OpenProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()
        # 一旦真的去抓取就立刻失败 —— 这条是本次功能的核心约束。
        self.collect_patch = patch.object(
            self.api, "_collect_videos",
            side_effect=AssertionError("打开本地记录不该触发抓取"))
        self.collect = self.collect_patch.start()
        self.addCleanup(self.collect_patch.stop)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write_archive(self, username, ids, complete=True, avatar="", owner=None,
                      last_sync=None):
        path = self.api._cache_file(username)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": 2, "avatar": avatar,
            "avatar_owner": username if owner is None else owner,
            "profile_stats": {"followers": "1.2M"},
            "videos": [video(i) for i in ids],
            "history_complete": complete,
            "last_sync": int(time.time()) if last_sync is None else last_sync,
            "count": len(ids),
        }, ensure_ascii=False), encoding="utf-8")
        return path

    def test_serves_the_archive_without_scraping(self):
        self.write_archive("owner", [1, 2, 3])
        result = self.api.open_profile("https://www.tiktok.com/@owner")
        self.assertTrue(result["ok"])
        self.assertTrue(result["cached"])
        self.assertEqual([v["id"] for v in result["videos"]], ["1", "2", "3"])
        self.assertEqual(result["username"], "owner")
        self.collect.assert_not_called()

    def test_a_video_link_resolves_to_its_owner(self):
        self.write_archive("owner", [7])
        result = self.api.open_profile("https://www.tiktok.com/@owner/video/7")
        self.assertTrue(result["ok"])
        self.assertEqual(result["username"], "owner")

    def test_url_with_query_and_trailing_slash_still_matches(self):
        self.write_archive("owner", [1])
        result = self.api.open_profile("https://www.tiktok.com/@owner/?lang=zh")
        self.assertTrue(result["ok"])

    def test_missing_archive_falls_through_to_a_real_scrape(self):
        result = self.api.open_profile("https://www.tiktok.com/@stranger")
        self.assertFalse(result["ok"])
        self.assertTrue(result["missing"])
        self.collect.assert_not_called()

    def test_archive_without_videos_counts_as_missing(self):
        self.write_archive("owner", [])
        self.assertFalse(self.api.open_profile("https://www.tiktok.com/@owner")["ok"])

    def test_incomplete_archive_opens_but_says_so(self):
        self.write_archive("owner", [1], complete=False)
        result = self.api.open_profile("https://www.tiktok.com/@owner")
        self.assertTrue(result["ok"], "上次没抓完不代表不能打开")
        self.assertFalse(result["complete"])
        # 提示语指向后台的「继续抓取」，而不是阻塞式的「重新抓取」
        self.assertIn("继续抓取", result["warning"])
        self.assertIn("后台", result["warning"])
        self.assertNotIn("重新抓取", result["warning"])
        self.assertIn("本地记录", result["message"])

    def test_complete_archive_has_nothing_to_warn_about(self):
        self.write_archive("owner", [1], complete=True)
        result = self.api.open_profile("https://www.tiktok.com/@owner")
        self.assertEqual(result["warning"], "")
        self.assertTrue(result["complete"])

    def test_status_line_reports_count_and_stamp(self):
        stamp = int(time.time()) - 3600
        self.write_archive("owner", [1, 2], last_sync=stamp)
        result = self.api.open_profile("https://www.tiktok.com/@owner")
        expected = time.strftime("%m-%d %H:%M", time.localtime(stamp))
        self.assertIn("2 条", result["message"])
        self.assertIn(expected, result["message"])
        self.assertEqual(result["lastSync"], stamp)

    def test_missing_timestamp_does_not_crash_the_message(self):
        path = self.write_archive("owner", [1])
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("last_sync")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = self.api.open_profile("https://www.tiktok.com/@owner")
        self.assertTrue(result["ok"])
        self.assertIn("时间未知", result["message"])

    def test_an_avatar_from_another_creator_is_not_reused(self):
        # 头像缓存串号时不能把别人的头像当成这个博主的
        self.write_archive("owner", [1], avatar="https://cdn.example/other.jpg",
                           owner="someone-else")
        result = self.api.open_profile("https://www.tiktok.com/@owner")
        self.assertEqual(result["avatar"], "")
        self.assertEqual(result["profileStats"], {})

    def test_own_avatar_and_stats_are_returned(self):
        self.write_archive("owner", [1], avatar="https://cdn.example/owner.jpg")
        result = self.api.open_profile("https://www.tiktok.com/@owner")
        self.assertEqual(result["avatar"], "https://cdn.example/owner.jpg")
        self.assertEqual(result["profileStats"], {"followers": "1.2M"})

    def test_a_non_tiktok_link_is_rejected_without_crashing(self):
        result = self.api.open_profile("https://example.com/@owner")
        self.assertFalse(result["ok"])
        self.assertIn("链接", result["error"])

    def test_recent_profiles_expose_count_and_completeness(self):
        self.write_archive("done-one", [1, 2], complete=True)
        self.write_archive("partial-one", [1], complete=False)
        rows = {row["username"]: row for row in self.api.recent_profiles()}
        self.assertEqual(rows["done-one"]["count"], 2)
        self.assertTrue(rows["done-one"]["complete"])
        self.assertFalse(rows["partial-one"]["complete"])


if __name__ == "__main__":
    unittest.main()
