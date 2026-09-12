import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from session_store import SessionStore, tiktok_cookies, has_session, chrome_app_bound
from web_app import Api


def cookie(name="sessionid", domain=".tiktok.com", expires=-1):
    return {"name": name, "value": "test-only-secret", "domain": domain,
            "path": "/", "secure": True, "httpOnly": True, "expires": expires}


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def write_cookies(self, *cookies):
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(str(Path(self.temp.name) / "cookies.txt"))
        for c in cookies:
            jar.set_cookie(http.cookiejar.Cookie(0,c["name"],c["value"],None,False,c["domain"],
                True,True,"/",True,True,None if c["expires"] < 0 else c["expires"],
                True,None,None,{},False))
        jar.save(ignore_discard=True, ignore_expires=True)
        return jar.filename

    def test_scope_expiration_and_session_detection(self):
        rows = [cookie(), cookie(domain="evil-tiktok.com"), cookie(domain="tiktok.com.evil.test"),
                cookie(domain=".www.tiktok.com"), cookie(expires=1)]
        self.assertEqual(len(tiktok_cookies(rows)), 2)
        self.assertTrue(has_session(rows))
        self.assertFalse(has_session([cookie("msToken")]))
        self.assertFalse(has_session([cookie(expires=1)]))

    def test_saved_login_encrypted_and_survives_restart(self):
        store = SessionStore(self.temp.name)
        store.save([cookie(),cookie(domain="example.com")], "test-agent")
        self.assertNotIn(b"test-only-secret", store.path.read_bytes())
        restored = SessionStore(self.temp.name).load()
        self.assertEqual(restored["cookies"], [cookie()])
        self.assertEqual(restored["user_agent"], "test-agent")

    def test_anonymous_cookies_cannot_replace_saved_login(self):
        store = SessionStore(self.temp.name)
        store.save([cookie()])
        before = store.path.read_bytes()
        with self.assertRaises(ValueError):
            store.save([cookie("msToken")])
        self.assertEqual(store.path.read_bytes(), before)

    def test_cookie_count_is_not_login_success(self):
        path = self.write_cookies(cookie("msToken"))
        status = self.api.set_cookie_options(path, "")
        self.assertEqual(status["count"], 1)
        self.assertFalse(status["ok"])
        self.assertFalse(status["hasSession"])
        self.assertTrue(status["error"])

    def test_failed_import_does_not_silently_scrape_anonymously(self):
        status = self.api.set_cookie_options("missing.txt", "")
        self.assertFalse(status["ok"])
        playwright = Mock()
        with self.assertRaises(RuntimeError):
            self.api._browser_context(playwright)
        playwright.chromium.launch.assert_not_called()

    def test_failed_login_source_does_not_return_stale_cache_as_success(self):
        cache = self.api._cache_file("owner")
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"avatar_owner": "owner", "videos": [{"id": "old"}]}))
        self.api.set_cookie_options("missing.txt", "")
        result = self.api.recognize("https://www.tiktok.com/@owner")
        self.assertFalse(result["ok"])
        self.assertIn("找不到 cookies.txt", result["error"])

    def test_download_and_scrape_share_tiktok_only_cookie_snapshot(self):
        path = self.write_cookies(cookie(), cookie(domain=".example.com"))
        status = self.api.set_cookie_options(path, "chrome")
        self.assertTrue(status["ok"])
        options = self.api._apply_cookie_options({"quiet": True, "cookiesfrombrowser": ("edge",)})
        self.assertNotIn("cookiesfrombrowser", options)
        self.assertNotIn("cookiefile", options)
        with self.api._youtube_dl(options) as ydl:
            from_download = {(c.domain,c.name,c.value) for c in ydl.cookiejar}
        from_scrape = {(c["domain"],c["name"],c["value"]) for c in self.api._require_cookies()}
        self.assertEqual(from_download, from_scrape)
        self.assertEqual(len(from_download), 1)

    def test_expiring_session_blocks_next_download(self):
        path = self.write_cookies(cookie(expires=int(time.time())+120))
        self.assertTrue(self.api.set_cookie_options(path, "")["ok"])
        self.api._cookie_snapshot[0]["expires"] = 1
        with self.assertRaises(RuntimeError):
            self.api._apply_cookie_options({})

    def test_app_bound_reports_real_problem_without_extracting(self):
        root = Path(self.temp.name)/"Google/Chrome/User Data"
        db = root/"Default/Network/Cookies"
        db.parent.mkdir(parents=True)
        (root/"Local State").write_text(json.dumps({"profile":{"last_active_profiles":["Default"]}}))
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, encrypted_value BLOB)")
            conn.execute("INSERT INTO cookies VALUES (?, ?, ?)", (".tiktok.com", "sessionid", b"v20test-only"))
        conn.close()
        self.assertTrue(chrome_app_bound(db.parent.parent))
        with patch("yt_dlp.cookies.extract_cookies_from_browser") as extract:
            status = self.api.set_cookie_options("", "chrome")
            extract.assert_not_called()
        self.assertFalse(status["ok"])
        self.assertEqual(status["code"], "app_bound")
        self.assertNotIn("test-only", status["error"])

    def test_explicit_anonymous_mode_is_allowed(self):
        self.assertTrue(self.api.set_cookie_options("", "")["ok"])
        self.assertEqual(self.api._require_cookies(), [])

    def test_saved_cookie_source_reads_snapshot(self):
        self.api._session_store.save([cookie()], "test-agent")
        status = self.api.set_cookie_options("", "saved")
        self.assertTrue(status["ok"])
        self.assertEqual(self.api._session_user_agent, "test-agent")
        self.assertEqual(self.api._require_cookies(), [cookie()])

    def test_double_click_does_not_open_multiple_login_windows(self):
        self.api._login_lock.acquire()
        self.api._login_busy = True
        with patch("web_app.threading.Thread") as thread:
            result = self.api.login_tiktok()
            thread.assert_not_called()
        self.assertTrue(result["busy"])
        self.api._login_lock.release()

    def test_collection_reuses_login_profile_without_overwriting_new_cookies(self):
        self.api._session_store.save([cookie()], "test-agent")
        self.api.set_cookie_options("", "saved")
        playwright = Mock()
        context = playwright.chromium.launch_persistent_context.return_value
        newer = {**cookie(),"value":"newer-test-only"}
        context.cookies.return_value = [newer]
        with patch("web_app.find_chrome", return_value="chrome.exe"):
            browser, returned = self.api._browser_context(playwright, reuse_login=True)
        self.assertIsNone(browser)
        self.assertIs(returned, context)
        self.assertEqual(playwright.chromium.launch_persistent_context.call_args.args[0],
                         str(self.api._session_store.root / "login-browser"))
        playwright.chromium.launch.assert_not_called()
        context.add_cookies.assert_not_called()

    def test_interactive_collection_keeps_saved_profile_visible(self):
        self.api._session_store.save([cookie()], "test-agent")
        self.api.set_cookie_options("", "saved")
        playwright = Mock()
        playwright.chromium.launch_persistent_context.return_value.cookies.return_value = [cookie()]
        with patch("web_app.find_chrome", return_value="chrome.exe"):
            self.api._browser_context(playwright, reuse_login=True, headless=False)
        self.assertFalse(playwright.chromium.launch_persistent_context.call_args.kwargs["headless"])

    def test_profile_archive_merges_rows_and_preserves_completion(self):
        self.api._profile_avatar = "avatar-a"
        first = self.api._store_profile_archive("owner", [{"id": "2", "title": "old"}], True)
        self.assertTrue(first["complete"])
        second = self.api._store_profile_archive("owner", [{"id": "3"}, {"id": "2", "title": "new"}], False)
        self.assertTrue(second["complete"])
        self.assertEqual([row["id"] for row in second["videos"]], ["3", "2"])
        self.assertEqual(second["videos"][1]["title"], "new")

    def test_verification_worker_does_not_callback_before_browser_collection(self):
        self.api._profile_lock.acquire()
        self.api._login_lock.acquire()
        self.api._login_busy = True
        with patch.object(self.api, "_collect_videos", return_value=[]), \
             patch.object(self.api, "_store_profile_archive", return_value={"ok": True}), \
             patch.object(self.api, "_emit") as emit:
            self.api._verify_profile_worker("owner", 12345)
        first_event = emit.call_args_list[0].args[0]
        self.assertEqual(first_event, "cookieStatus")
        self.assertFalse(self.api.get_verification_status()["busy"])

    def test_login_cannot_reopen_profile_while_collection_uses_it(self):
        self.api._profile_lock.acquire()
        try:
            with patch("web_app.threading.Thread") as thread:
                status = self.api.login_tiktok()
            self.assertFalse(status["ok"])
            thread.assert_not_called()
            self.assertFalse(self.api._login_lock.locked())
        finally:
            self.api._profile_lock.release()

    def test_verification_only_targets_current_challenged_profile(self):
        self.api._verification_username = "apple"
        with patch("web_app.threading.Thread") as thread, \
             patch("web_app.subprocess.Popen"), patch("web_app.find_chrome", return_value="chrome.exe"), \
             patch.object(self.api._verification_started, "wait", return_value=True):
            self.api._verification_status = {"busy": True, "stage": "opened"}
            self.assertFalse(self.api.verify_profile("other")["ok"])
            self.assertFalse(self.api.verify_profile("apple/../../other")["ok"])
            thread.assert_not_called()
            self.assertTrue(self.api.verify_profile("apple")["ok"])
            self.assertEqual(thread.call_args.kwargs["args"][0], "apple")
            self.assertIsInstance(thread.call_args.kwargs["args"][1], int)
        self.api._profile_lock.release()
        self.api._login_lock.release()


if __name__ == "__main__":
    unittest.main()
