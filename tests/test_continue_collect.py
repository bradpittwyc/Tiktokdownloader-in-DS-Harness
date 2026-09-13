"""「继续抓取」：后台补齐归档，绝不阻塞界面。

核心不变量：continue_collect 必须立刻返回（界面不等它），
结果通过 backgroundCollect / archiveUpdate 事件推回来。
"""

import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from web_app import Api  # noqa: E402


def video(ident):
    return {"id": str(ident), "url": f"https://www.tiktok.com/@owner/video/{ident}",
            "title": f"作品 {ident}", "type": "video"}


class ContinueCollectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()
        self.api._cookie_browser = ""
        self.events = []
        self.api._emit = lambda function, value: self.events.append((function, value))
        self.collected = []

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def fake_collect(self, result=None, error=None, delay=0.0):
        def collect(url, interactive=False, lock_held=False, cdp_url=None):
            self.collected.append({"url": url, "lock_held": lock_held})
            if delay:
                time.sleep(delay)
            if error:
                raise error
            return result if result is not None else []
        self.api._collect_videos = collect

    def background_events(self):
        return [value for function, value in self.events if function == "backgroundCollect"]

    def wait_for_finish(self, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for event in self.background_events():
                if not event.get("running"):
                    return event
            time.sleep(.02)
        self.fail("后台抓取没有在超时内结束")

    def test_returns_immediately_instead_of_waiting_for_the_scrape(self):
        self.fake_collect(result=[video(1)], delay=1.5)
        started = time.monotonic()
        result = self.api.continue_collect("https://www.tiktok.com/@owner")
        elapsed = time.monotonic() - started
        self.assertTrue(result["ok"])
        self.assertTrue(result["started"])
        self.assertLess(elapsed, 0.3,
                        "continue_collect 必须立刻返回，否则界面就被卡住了")
        self.wait_for_finish()

    def test_reports_running_then_finished(self):
        self.fake_collect(result=[video(1), video(2)])
        self.api.continue_collect("https://www.tiktok.com/@owner")
        final = self.wait_for_finish()
        self.assertTrue(self.background_events()[0]["running"])
        self.assertFalse(final["running"])
        self.assertEqual(final["count"], 2)
        self.assertEqual(final["username"], "owner")

    def test_results_land_in_the_archive(self):
        self.fake_collect(result=[video(1), video(2)])
        self.api.continue_collect("https://www.tiktok.com/@owner")
        self.wait_for_finish()
        archive = self.api._load_profile_archive("owner")
        self.assertEqual({v["id"] for v in archive["videos"]}, {"1", "2"})

    def test_the_background_run_holds_the_profile_lock_itself(self):
        self.fake_collect(result=[video(1)])
        self.api.continue_collect("https://www.tiktok.com/@owner")
        self.wait_for_finish()
        self.assertEqual(self.collected[0]["lock_held"], True,
                         "锁要由 continue_collect 自己拿，不能再让 _collect_videos 抢一次")

    def test_busy_when_a_foreground_scrape_is_running(self):
        self.fake_collect(result=[])
        self.assertTrue(self.api._profile_lock.acquire(blocking=False))
        try:
            result = self.api.continue_collect("https://www.tiktok.com/@owner")
        finally:
            self.api._profile_lock.release()
        self.assertFalse(result["ok"])
        self.assertTrue(result["busy"])
        self.assertEqual(self.collected, [])

    def test_busy_when_another_background_run_is_active(self):
        self.fake_collect(result=[video(1)], delay=0.6)
        self.assertTrue(self.api.continue_collect("https://www.tiktok.com/@owner")["ok"])
        second = self.api.continue_collect("https://www.tiktok.com/@owner")
        self.assertFalse(second["ok"])
        self.assertTrue(second["busy"])
        self.wait_for_finish()

    def test_locks_are_released_so_the_next_run_works(self):
        self.fake_collect(result=[video(1)])
        self.api.continue_collect("https://www.tiktok.com/@owner")
        self.wait_for_finish()
        for lock in ("_background_collect_lock", "_profile_lock"):
            acquired = getattr(self.api, lock).acquire(blocking=False)
            self.assertTrue(acquired, f"{lock} 没有被释放，下一次抓取会被永久挡住")
            getattr(self.api, lock).release()

    def test_a_failure_is_reported_and_still_releases_the_locks(self):
        self.fake_collect(error=RuntimeError("TikTok 没有返回可读取的作品"))
        self.api.continue_collect("https://www.tiktok.com/@owner")
        final = self.wait_for_finish()
        self.assertIn("没有返回可读取", final["error"])
        self.assertTrue(self.api._background_collect_lock.acquire(blocking=False))
        self.api._background_collect_lock.release()

    def test_a_bad_link_is_rejected_without_starting_anything(self):
        result = self.api.continue_collect("https://example.com/@owner")
        self.assertFalse(result["ok"])
        self.assertEqual(self.background_events(), [])

    def test_it_scrapes_the_owner_profile(self):
        self.fake_collect(result=[video(1)])
        self.api.continue_collect("https://www.tiktok.com/@owner/video/1")
        self.wait_for_finish()
        self.assertEqual(self.collected[0]["url"], "https://www.tiktok.com/@owner")


if __name__ == "__main__":
    unittest.main()
