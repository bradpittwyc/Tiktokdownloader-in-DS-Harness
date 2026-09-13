"""回归网：webview 必须以持久模式启动。

pywebview 的 private_mode 默认是 True，在 Windows 上会让 EdgeChromium 用临时
profile，localStorage 下次启动就没了 —— 默认保存目录、并发数、重试次数、
日期范围、多博主链接、下载记录、学习笔记全部静默重置。

真机实测（两个独立进程，file:// 页面）：
    private_mode=True  -> 写入成功，下次启动读回 None
    private_mode=False -> 写入成功，下次启动读回 'VALUE-123'
"""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from web_app import Api, start_ui, webview_storage_path  # noqa: E402


class WebviewPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def start(self):
        with patch("web_app.webview.create_window") as create, \
                patch("web_app.webview.start") as start:
            start_ui(self.api)
        return create, start

    def test_private_mode_must_be_off(self):
        _, start = self.start()
        self.assertIn("private_mode", start.call_args.kwargs,
                      "必须显式关掉 private_mode：默认值是 True，会让所有偏好丢失")
        self.assertFalse(start.call_args.kwargs["private_mode"])

    def test_a_stable_storage_path_is_given(self):
        _, start = self.start()
        storage = start.call_args.kwargs.get("storage_path")
        self.assertTrue(storage, "没有 storage_path，EdgeChromium 会退回临时目录")
        self.assertEqual(Path(storage), webview_storage_path())

    def test_storage_path_lives_under_the_app_data_folder(self):
        # 必须落在应用自己的目录下，而不是别处或临时目录
        path = webview_storage_path()
        self.assertEqual(path.name, "webview")
        self.assertEqual(path.parent.name, "TikTokBatchMVP")
        self.assertTrue(str(path).startswith(self.temp.name))

    def test_storage_path_is_not_the_default_shared_pywebview_folder(self):
        # pywebview 在 private_mode=False 但没给路径时会用 %APPDATA%\pywebview，
        # 那是所有 pywebview 应用共用的，不该退回到那里
        self.assertNotEqual(webview_storage_path().name, "pywebview")

    def test_the_window_is_still_wired_to_the_api(self):
        create, _ = self.start()
        self.assertIs(self.api._window, create.return_value,
                      "js_api 依赖 api._window，不能在重构里弄丢")
        self.assertIs(create.call_args.kwargs["js_api"], self.api)


if __name__ == "__main__":
    unittest.main()
