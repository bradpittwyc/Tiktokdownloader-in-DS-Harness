"""残留 Chrome 的清理。

真机现场：应用关掉时后台抓取线程（daemon）连同 Chrome 一起被丢下，
下一个 Chrome 启动时把命令行交接给残留实例后自杀 —— 调试端口永远不开，
25 秒后报一个莫名其妙的 ECONNREFUSED。日志里连中两次。

探测依据（真机实测）：
  没有 Chrome -> profile/lockfile 不存在
  Chrome 运行中 -> lockfile 存在且打开会 raise PermissionError
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from web_app import Api  # noqa: E402


class ProfileInUseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()
        self.profile = self.api._login_profile_dir()
        self.profile.mkdir(parents=True, exist_ok=True)
        self.lock = self.profile / "lockfile"

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_no_lockfile_means_free(self):
        self.assertFalse(self.lock.exists())
        self.assertFalse(self.api._profile_in_use())

    def test_an_openable_lockfile_means_free(self):
        self.lock.write_bytes(b"")
        self.assertFalse(self.api._profile_in_use())

    def test_an_exclusively_held_lockfile_means_in_use(self):
        # 模拟 Chrome 独占持有：打开就失败
        self.lock.write_bytes(b"")
        with patch("builtins.open", side_effect=PermissionError("held by Chrome")):
            self.assertTrue(self.api._profile_in_use())

    def test_a_vanished_profile_does_not_raise(self):
        self.lock.unlink(missing_ok=True)
        self.assertFalse(self.api._profile_in_use())


class KillChromeOnProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_it_kills_exactly_the_pids_the_profile_query_returned(self):
        query = Mock(stdout="111\n222\n", returncode=0)
        with patch("web_app.subprocess.run", side_effect=[query, Mock(), Mock()]) as run:
            killed = self.api._kill_chrome_on_profile()
        self.assertEqual(killed, 2)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["taskkill", "/PID", "111", "/T", "/F"], commands)
        self.assertIn(["taskkill", "/PID", "222", "/T", "/F"], commands)
        # 绝不能出现"按名字杀 chrome"这种会误伤用户自己浏览器的东西
        for command in commands:
            self.assertNotIn("/IM", command)
            self.assertNotIn("chrome.exe", command)

    def test_the_query_is_scoped_to_our_own_profile(self):
        query = Mock(stdout="", returncode=0)
        with patch("web_app.subprocess.run", side_effect=[query]) as run:
            self.api._kill_chrome_on_profile()
        script = run.call_args_list[0].args[0][-1]
        self.assertIn(str(self.api._login_profile_dir()), script,
                      "必须按 user-data-dir 精确匹配，否则会杀掉用户自己的 Chrome")

    def test_no_matches_kills_nothing(self):
        query = Mock(stdout="", returncode=0)
        with patch("web_app.subprocess.run", side_effect=[query]) as run:
            self.assertEqual(self.api._kill_chrome_on_profile(), 0)
        self.assertEqual(len(run.call_args_list), 1, "只该跑查询，不该跑 taskkill")

    def test_garbage_output_is_ignored(self):
        query = Mock(stdout="not-a-pid\n\n", returncode=0)
        with patch("web_app.subprocess.run", side_effect=[query]) as run:
            self.assertEqual(self.api._kill_chrome_on_profile(), 0)
        self.assertEqual(len(run.call_args_list), 1)

    def test_a_failing_query_is_reported_not_raised(self):
        with patch("web_app.subprocess.run", side_effect=OSError("powershell missing")):
            self.assertEqual(self.api._kill_chrome_on_profile(), 0)


class LaunchCleansStaleChromeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_launch_clears_a_stale_chrome_before_starting(self):
        with patch.object(self.api, "_profile_in_use", side_effect=[True, False]), \
                patch.object(self.api, "_kill_chrome_on_profile") as kill, \
                patch("web_app.find_chrome", return_value=r"C:\chrome.exe"), \
                patch("web_app.subprocess.Popen") as popen:
            self.api._launch_native_chrome("https://www.tiktok.com/@owner")
        kill.assert_called_once()

    def test_launch_does_not_touch_chrome_when_the_profile_is_free(self):
        with patch.object(self.api, "_profile_in_use", return_value=False), \
                patch.object(self.api, "_kill_chrome_on_profile") as kill, \
                patch("web_app.find_chrome", return_value=r"C:\chrome.exe"), \
                patch("web_app.subprocess.Popen"):
            self.api._launch_native_chrome("https://www.tiktok.com/@owner")
        kill.assert_not_called()

    def test_a_stubborn_chrome_does_not_block_the_launch_forever(self):
        # 清理之后仍然占用也不能死循环，最多等 5 秒就照常启动
        with patch.object(self.api, "_profile_in_use", return_value=True), \
                patch.object(self.api, "_kill_chrome_on_profile"), \
                patch("web_app.time.sleep") as sleep, \
                patch("web_app.find_chrome", return_value=r"C:\chrome.exe"), \
                patch("web_app.subprocess.Popen") as popen:
            self.api._launch_native_chrome("https://www.tiktok.com/@owner")
        self.assertTrue(popen.called)
        self.assertLessEqual(sleep.call_count, 20)


if __name__ == "__main__":
    unittest.main()
