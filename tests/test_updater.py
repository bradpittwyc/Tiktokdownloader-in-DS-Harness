"""自动升级：版本比较、GitHub release 解析、下载与安装。

全部离线：网络请求一律打桩，不发真实请求。
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from web_app import Api, app_version, version_tuple  # noqa: E402


def fake_release(tag="v9.9.9", with_asset=True, body="更新说明正文"):
    assets = []
    if with_asset:
        assets.append({"name": f"TikTokBatchMVP-Setup-{tag.lstrip('v')}.exe", "size": 1234,
                       "url": f"https://api.github.com/repos/x/releases/assets/1",
                       "browser_download_url": f"https://github.com/x/releases/download/{tag}/setup.exe"})
    assets.append({"name": "TikTokBatchMVP.exe", "size": 999,
                   "url": "https://api.github.com/repos/x/releases/assets/2",
                   "browser_download_url": "https://github.com/x/releases/download/portable.exe"})
    return {"tag_name": tag, "body": body, "published_at": "2026-09-13T00:00:00Z",
            "html_url": f"https://github.com/x/releases/tag/{tag}", "assets": assets}


def response(status=200, payload=None, headers=None, chunks=None):
    item = Mock()
    item.status_code = status
    item.headers = headers or {}
    item.json.return_value = payload if payload is not None else {}
    item.iter_content.return_value = iter(chunks or [])
    item.raise_for_status = Mock()
    item.close = Mock()
    return item


class VersionTests(unittest.TestCase):
    def test_numeric_comparison_not_string(self):
        # 字符串比较会得出 '1.0.10' < '1.0.2'，必须按数字段比
        self.assertLess(version_tuple("1.0.2"), version_tuple("1.0.10"))
        self.assertGreater(version_tuple("2.0.0"), version_tuple("1.99.99"))

    def test_v_prefix_and_short_forms(self):
        self.assertEqual(version_tuple("v1.2.3"), (1, 2, 3))
        self.assertEqual(version_tuple("1.2"), (1, 2))
        self.assertEqual(version_tuple(""), (0,))

    def test_app_version_reads_the_version_file(self):
        self.assertRegex(app_version(), r"^\d+\.\d+")


class TokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_token_round_trips_encrypted(self):
        self.assertFalse(self.api.get_update_info()["tokenSet"])
        self.assertTrue(self.api.set_update_token("ghp_example_token")["ok"])
        path = self.api._update_token_file()
        self.assertTrue(path.is_file())
        # 落盘的必须是密文，不能出现明文
        self.assertNotIn(b"ghp_example_token", path.read_bytes())
        self.assertEqual(self.api._load_update_token(), "ghp_example_token")

    def test_a_new_instance_reads_the_saved_token(self):
        self.api.set_update_token("ghp_persisted")
        self.assertEqual(Api()._update_token, "ghp_persisted")

    def test_empty_token_clears_it(self):
        self.api.set_update_token("ghp_x")
        self.api.set_update_token("")
        self.assertFalse(self.api.get_update_info()["tokenSet"])
        self.assertFalse(self.api._update_token_file().exists())

    def test_headers_carry_the_token_only_when_set(self):
        self.assertNotIn("Authorization", self.api._github_headers())
        self.api._update_token = "ghp_y"
        self.assertEqual(self.api._github_headers()["Authorization"], "Bearer ghp_y")


class CheckUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def check(self, reply):
        with patch("web_app.requests.get", return_value=reply):
            return self.api.check_update()

    def test_newer_release_is_detected(self):
        result = self.check(response(200, fake_release("v99.0.0")))
        self.assertTrue(result["ok"])
        self.assertEqual(result["latest"], "99.0.0")
        self.assertTrue(result["hasUpdate"])

    def test_same_version_is_not_an_update(self):
        result = self.check(response(200, fake_release(f"v{app_version()}")))
        self.assertTrue(result["ok"])
        self.assertFalse(result["hasUpdate"])

    def test_older_release_is_not_an_update(self):
        result = self.check(response(200, fake_release("v0.0.1")))
        self.assertFalse(result["hasUpdate"])

    def test_the_setup_asset_is_picked_not_the_portable_exe(self):
        result = self.check(response(200, fake_release("v99.0.0")))
        self.assertIn("Setup", result["asset"]["name"])
        self.assertIn("api.github.com", result["asset"]["url"])

    def test_release_without_a_setup_asset_still_reports(self):
        result = self.check(response(200, fake_release("v99.0.0", with_asset=False)))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["asset"])

    def test_404_means_token_needed_not_missing_release(self):
        # 私有仓库对未认证请求就是 404
        result = self.check(response(404))
        self.assertFalse(result["ok"])
        self.assertTrue(result["needsToken"])
        self.assertIn("Token", result["error"])

    def test_auth_failure_is_reported(self):
        for status in (401, 403):
            result = self.check(response(status))
            self.assertFalse(result["ok"])
            self.assertIn("Token", result["error"])

    def test_unexpected_status_is_reported(self):
        self.assertIn("500", self.check(response(500))["error"])

    def test_network_error_is_not_fatal(self):
        with patch("web_app.requests.get", side_effect=OSError("no network")):
            result = self.api.check_update()
        self.assertFalse(result["ok"])
        self.assertIn("无法连接", result["error"])

    def test_unparsable_body_is_reported(self):
        broken = response(200)
        broken.json.side_effect = ValueError("not json")
        self.assertIn("无法解析", self.check(broken)["error"])


class DownloadUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name, "TEMP": self.temp.name})
        self.env.start()
        self.api = Api()
        self.events = []
        self.api._emit = lambda function, value: self.events.append((function, value))

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_downloads_the_setup_and_reports_progress(self):
        chunks = [b"x" * 1024, b"y" * 2048]
        replies = [response(200, fake_release("v99.0.0")),
                   response(200, headers={"Content-Length": str(3072)}, chunks=chunks)]
        with patch("web_app.requests.get", side_effect=replies):
            result = self.api.download_update()
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["version"], "99.0.0")
        self.assertEqual(Path(result["path"]).read_bytes(), b"x" * 1024 + b"y" * 2048)
        progress = [v for f, v in self.events if f == "updateProgress"]
        self.assertTrue(progress)
        self.assertTrue(progress[-1].get("done"))
        self.assertEqual(progress[-1]["percent"], 100)
        Path(result["path"]).unlink(missing_ok=True)

    def test_refuses_when_already_current(self):
        with patch("web_app.requests.get", return_value=response(200, fake_release(f"v{app_version()}"))):
            result = self.api.download_update()
        self.assertFalse(result["ok"])
        self.assertIn("最新版本", result["error"])

    def test_no_asset_is_reported(self):
        with patch("web_app.requests.get", return_value=response(200, fake_release("v99.0.0", with_asset=False))):
            result = self.api.download_update()
        self.assertFalse(result["ok"])
        self.assertIn("没有安装包", result["error"])

    def test_a_failed_download_leaves_no_partial_file(self):
        replies = [response(200, fake_release("v99.0.0")), response(404)]
        with patch("web_app.requests.get", side_effect=replies):
            result = self.api.download_update()
        self.assertFalse(result["ok"])
        leftovers = list(Path(self.temp.name).glob("TikTokBatchMVP-Setup-*"))
        self.assertEqual(leftovers, [], "下载失败不能留下半截文件")


class InstallUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_missing_file_is_rejected(self):
        self.assertFalse(self.api.install_update(str(Path(self.temp.name) / "nope.exe"))["ok"])

    def test_non_exe_is_rejected(self):
        fake = Path(self.temp.name) / "setup.zip"
        fake.write_bytes(b"x")
        result = self.api.install_update(str(fake))
        self.assertFalse(result["ok"])
        self.assertIn("格式", result["error"])

    def test_a_valid_installer_is_launched_and_the_app_is_closed(self):
        exe = Path(self.temp.name) / "TikTokBatchMVP-Setup-9.9.9.exe"
        exe.write_bytes(b"MZ")
        with patch("web_app.subprocess.Popen") as popen, \
                patch("web_app.threading.Timer") as timer:
            result = self.api.install_update(str(exe))
        self.assertTrue(result["ok"])
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], [str(exe)])
        timer.assert_called_once()
        # 定时器必须设成 daemon，否则会拖住进程退出
        self.assertTrue(timer.return_value.daemon)


if __name__ == "__main__":
    unittest.main()
