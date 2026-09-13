"""验证窗口关闭后自动交接给后台抓取的回归测试。

覆盖三块：
  1. 挑战页识别（page_challenged）—— 滑块在 iframe 里，且 frame 会随时 detach
  2. 窗口是否还在（_window_gone）—— 真报错必须冒泡，不能被当成"用户关窗"吞掉
  3. _verify_profile_worker 的交接决策 —— 什么时候该转后台，什么时候该老实报错
"""

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from web_app import Api, CHALLENGE_MARKERS, page_challenged


class FakeFrame:
    def __init__(self, text="", detached=False):
        self._text = text
        self._detached = detached

    def locator(self, _selector):
        frame = self

        class Locator:
            def inner_text(self, timeout=None):
                if frame._detached:
                    raise RuntimeError("Frame was detached")
                return frame._text

        return Locator()


class FakePage:
    def __init__(self, frames=(), closed=False, is_closed_raises=False):
        self.frames = list(frames)
        self._closed = closed
        self._raises = is_closed_raises
        self.closed_calls = 0

    def is_closed(self):
        self.closed_calls += 1
        if self._raises:
            raise RuntimeError("Target page, context or browser has been closed")
        return self._closed


def tiktok_session(name="sessionid", value="secret"):
    return {"name": name, "value": value, "domain": ".tiktok.com", "path": "/",
            "secure": True, "httpOnly": True, "expires": -1}


class FakeContext:
    def __init__(self, cookies=None, raises=False):
        self._cookies = list(cookies or [])
        self._raises = raises

    def cookies(self):
        if self._raises:
            raise RuntimeError("Target page, context or browser has been closed")
        return self._cookies


class FakeProcess:
    """Stand-in for subprocess.Popen covering the ways Chrome can refuse to die."""

    def __init__(self, alive=True, honors_terminate=True):
        self.alive = alive
        self.honors_terminate = honors_terminate
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        if self.honors_terminate:
            self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False

    def wait(self, timeout=None):
        self.alive = False
        return 0


class ChallengeDetectionTests(unittest.TestCase):
    def test_marker_inside_a_child_frame_is_found(self):
        # The slider renders in an injected iframe, not the top document.
        page = FakePage([FakeFrame("Loading…"), FakeFrame("Drag the slider to fit the puzzle")])
        self.assertTrue(page_challenged(page))

    def test_chinese_marker_is_found(self):
        self.assertTrue(page_challenged(FakePage([FakeFrame("请拖动滑块完成验证")])))

    def test_clean_profile_page_is_not_a_challenge(self):
        self.assertFalse(page_challenged(FakePage([FakeFrame("12.3K Followers")])))

    def test_detached_frame_is_skipped_not_fatal(self):
        page = FakePage([FakeFrame(detached=True), FakeFrame("verify to continue")])
        self.assertTrue(page_challenged(page))

    def test_all_frames_detached_means_no_challenge(self):
        self.assertFalse(page_challenged(FakePage([FakeFrame(detached=True)])))

    def test_marker_list_still_covers_both_languages(self):
        self.assertIn("拖动滑块", CHALLENGE_MARKERS)
        self.assertIn("drag the slider to fit the puzzle", CHALLENGE_MARKERS)


class WindowGoneTests(unittest.TestCase):
    def test_no_page_yet_is_not_a_closed_window(self):
        self.assertFalse(Api._window_gone(None))

    def test_open_page_is_not_gone(self):
        self.assertFalse(Api._window_gone(FakePage(closed=False)))

    def test_closed_page_is_gone(self):
        self.assertTrue(Api._window_gone(FakePage(closed=True)))

    def test_a_raising_page_is_not_reported_as_user_closed(self):
        # Real failures must keep propagating, otherwise they get silently
        # rewritten into "the user closed the window, continuing in background".
        self.assertFalse(Api._window_gone(FakePage(is_closed_raises=True)))


class CloseLoginBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_missing_process_is_a_noop(self):
        self.api._login_process = None
        self.api._close_login_browser(timeout=0.2)

    def test_already_exited_browser_is_left_alone(self):
        process = FakeProcess(alive=False)
        self.api._login_process = process
        self.api._close_login_browser(timeout=0.2)
        self.assertFalse(process.terminated)

    def test_running_browser_is_terminated(self):
        process = FakeProcess(alive=True)
        self.api._login_process = process
        started = time.monotonic()
        self.api._close_login_browser(timeout=5)
        self.assertTrue(process.terminated)
        self.assertFalse(process.alive)
        self.assertFalse(process.killed)
        self.assertLess(time.monotonic() - started, 3)

    def test_unresponsive_browser_is_killed(self):
        # Experiment: browser.close() does not stop Chrome, so a browser that
        # ignores terminate() would keep holding the user-data-dir lock.
        process = FakeProcess(alive=True, honors_terminate=False)
        self.api._login_process = process
        self.api._close_login_browser(timeout=0.3)
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertFalse(process.alive)


class SnapshotLiveCookiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()
        self.api._cookie_browser = "saved"

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_saves_the_live_session(self):
        self.api._snapshot_live_cookies(FakeContext([tiktok_session()]))
        self.assertTrue(self.api._session_store.path.exists())
        self.assertEqual([c["name"] for c in self.api._cookie_snapshot], ["sessionid"])

    def test_skips_persisting_when_the_cookie_source_is_not_the_saved_session(self):
        # Still remembered in memory, just not written to the DPAPI file.
        self.api._cookie_browser = "chrome"
        self.api._snapshot_live_cookies(FakeContext([tiktok_session()]))
        self.assertFalse(self.api._session_store.path.exists())
        self.assertEqual([c["name"] for c in self.api._verified_cookies], ["sessionid"])

    def test_skips_when_a_cookie_file_is_in_use(self):
        self.api._cookie_file = "cookies.txt"
        self.api._snapshot_live_cookies(FakeContext([tiktok_session()]))
        self.assertFalse(self.api._session_store.path.exists())

    def test_closed_context_does_not_raise(self):
        # This is the normal path when the user shuts the window.
        self.api._snapshot_live_cookies(FakeContext(raises=True))
        self.assertFalse(self.api._session_store.path.exists())

    def test_context_without_a_login_is_not_persisted(self):
        self.api._snapshot_live_cookies(FakeContext([tiktok_session("ttwid")]))
        self.assertFalse(self.api._session_store.path.exists())


class VerifiedProfileTests(unittest.TestCase):
    """验证过的 profile 必须被复用 —— 这是关窗后还能继续抓的前提。

    真机现场：用户 cookie 来源不是「软件保存的登录态」，而 verify_profile
    永远开的是 app 自有 profile（实测该 profile 是完整登录态：sessionid /
    sid_guard / tt_chain_token 等 31 条）。交接时若不强制回到它，
    就等于另开一个 TikTok 从没放行过的浏览器，必然又被挑战。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()
        # 绕开真实浏览器 cookie 提取：直接给一份内存快照
        self.api._cookie_browser = "chrome"
        self.api._cookie_loaded = True
        self.api._cookie_snapshot = [tiktok_session()]

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def fake_playwright(self, profile_cookies):
        context = Mock()
        context.cookies.return_value = list(profile_cookies)
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value = context
        return playwright, context

    def test_force_profile_wins_over_the_cookie_source(self):
        playwright, context = self.fake_playwright([])
        browser, ctx = self.api._browser_context(playwright, reuse_login=True,
                                                 headless=True, force_profile=True)
        self.assertIsNone(browser)
        self.assertIs(ctx, context)
        playwright.chromium.launch_persistent_context.assert_called_once()
        playwright.chromium.launch.assert_not_called()

    def test_without_force_a_non_saved_source_still_uses_a_fresh_browser(self):
        # 普通抓取路径不受影响：来源不是 saved 时照旧开新浏览器。
        playwright, context = self.fake_playwright([])
        self.api._browser_context(playwright, reuse_login=True,
                                  headless=True, force_profile=False)
        playwright.chromium.launch_persistent_context.assert_not_called()
        playwright.chromium.launch.assert_called_once()

    def test_restores_missing_cookies_even_when_the_session_survived(self):
        # 回归：以前只在 has_session 为假时才补 cookie，于是
        # 「sessionid 还在、验证 clearance 掉了」这种情况永远补不回来。
        self.api._verified_cookies = [tiktok_session("tt_csrf_token", "SOLVED")]
        playwright, context = self.fake_playwright([tiktok_session()])
        self.api._browser_context(playwright, reuse_login=True, headless=True,
                                  force_profile=True)
        added = context.add_cookies.call_args[0][0]
        self.assertEqual([c["name"] for c in added], ["tt_csrf_token"])

    def test_cookies_the_profile_already_has_are_not_rewritten(self):
        self.api._verified_cookies = [tiktok_session("sessionid", "FRESH")]
        playwright, context = self.fake_playwright([tiktok_session("sessionid", "IN-PROFILE")])
        self.api._browser_context(playwright, reuse_login=True, headless=True,
                                  force_profile=True)
        context.add_cookies.assert_not_called()


class VerifyWorkerHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()
        self.api._cookie_browser = "saved"
        self.calls = []

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def run_worker(self, collect):
        self.api._collect_videos = collect
        # The real worker is entered from verify_profile with both locks held.
        self.api._profile_lock.acquire()
        self.api._login_lock.acquire()
        self.api._login_process = None
        with patch.object(self.api, "_close_login_browser"):
            self.api._verify_profile_worker("owner", 9222)
        return self.api._verification_status

    def test_closing_the_window_continues_in_a_background_pass(self):
        def collect(url, interactive=False, lock_held=False, cdp_url=None, force_profile=False):
            self.calls.append({"interactive": interactive, "cdp_url": cdp_url,
                               "force_profile": force_profile})
            if interactive:
                self.api._collection_window_closed = True
                self.api._collection_warning = "验证窗口已关闭"
                return [{"id": "1", "title": "a"}, {"id": "2", "title": "b"}]
            self.api._collection_complete = True
            return [{"id": "2", "title": "b"}, {"id": "3", "title": "c"}]

        status = self.run_worker(collect)

        self.assertEqual(len(self.calls), 2, "关窗后必须再跑一遍后台抓取")
        self.assertTrue(self.calls[0]["interactive"])
        self.assertIsNone(self.calls[1]["cdp_url"], "第二遍不能再用 CDP 挂可见窗口")
        self.assertFalse(self.calls[1]["interactive"])
        self.assertFalse(self.calls[0]["force_profile"], "可见窗口阶段不该强制 profile")
        self.assertTrue(self.calls[1]["force_profile"],
                        "后台那遍必须回到验证过的 profile，否则验证等于白做")
        self.assertTrue(status["ready"])
        archive = self.api._load_profile_archive("owner")
        self.assertEqual({v["id"] for v in archive["videos"]}, {"1", "2", "3"},
                         "两遍抓到的结果必须按 id 合并，不能覆盖")
        self.assertTrue(archive["history_complete"])

    def test_teardown_error_after_close_still_hands_off(self):
        # Closing the window makes Playwright throw mid-evaluate; that is not a
        # failure, it is the hand-off trigger.
        def collect(url, interactive=False, lock_held=False, cdp_url=None, force_profile=False):
            self.calls.append(interactive)
            if interactive:
                self.api._collection_window_closed = True
                raise RuntimeError("Target page, context or browser has been closed")
            self.api._collection_complete = True
            return [{"id": "9", "title": "recovered"}]

        status = self.run_worker(collect)

        self.assertEqual(self.calls, [True, False])
        self.assertTrue(status["ready"])
        self.assertEqual(status["error"], "")
        self.assertEqual([v["id"] for v in self.api._load_profile_archive("owner")["videos"]], ["9"])

    def test_real_failure_is_still_reported_and_not_retried(self):
        def collect(url, interactive=False, lock_held=False, cdp_url=None, force_profile=False):
            self.calls.append(interactive)
            raise RuntimeError("等待 TikTok 验证超时")

        status = self.run_worker(collect)

        self.assertEqual(self.calls, [True], "非关窗错误不该触发后台重跑")
        self.assertFalse(status["ready"])
        self.assertIn("超时", status["error"])

    def test_completed_visible_run_does_not_start_a_second_pass(self):
        def collect(url, interactive=False, lock_held=False, cdp_url=None, force_profile=False):
            self.calls.append(interactive)
            self.api._collection_complete = True
            return [{"id": "1", "title": "a"}]

        status = self.run_worker(collect)

        self.assertEqual(self.calls, [True])
        self.assertTrue(status["ready"])

    def test_failed_handoff_keeps_the_archive_from_the_visible_run(self):
        def collect(url, interactive=False, lock_held=False, cdp_url=None, force_profile=False):
            self.calls.append(interactive)
            if interactive:
                self.api._collection_window_closed = True
                return [{"id": "1", "title": "kept"}]
            raise RuntimeError("验证态没保存住")

        status = self.run_worker(collect)

        self.assertEqual(self.calls, [True, False])
        self.assertTrue(status["ready"], "交接失败不能让整个验证流程变成失败")
        self.assertEqual([v["id"] for v in self.api._load_profile_archive("owner")["videos"]], ["1"])


if __name__ == "__main__":
    unittest.main()
