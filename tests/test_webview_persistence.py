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
from web_app import (  # noqa: E402
    Api,
    WINDOW_TITLE,
    app_version,
    apply_window_icon,
    start_ui,
    webview_storage_path,
)

ICON_PATH = (Path(__file__).resolve().parents[1]
             / "outputs/TikTokBatchMVP/ui/tiktok-logo.ico")


class WindowTitleTests(unittest.TestCase):
    """标题栏左上角的版本号。

    pywebview 的 winforms 后端只在建窗时给 `self.Text` 赋一次值
    （winforms.py:197），整个包里没有任何 DocumentTitleChanged 处理 ——
    所以 HTML 的 <title> 不会覆盖原生标题栏，标题完全由 WINDOW_TITLE 决定。
    这也意味着版本号必须写进这个常量，改 index.html 是没用的。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_the_title_carries_the_real_version(self):
        version = app_version()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$",
                         "VERSION 读不到时是 0.0.0，这里的断言会连带暴露打包漏带 VERSION")
        self.assertEqual(WINDOW_TITLE, f"TikTok 下载器 {version}")

    def test_the_title_is_not_the_stale_bare_name(self):
        # 回归网：改回不带版本号的旧标题就应该失败（索引页自己不做这件事）
        self.assertNotEqual(WINDOW_TITLE, "TikTok 下载器")

    def test_the_version_in_the_title_is_this_build_s_not_a_hardcoded_one(self):
        version_file = (Path(__file__).resolve().parents[1]
                        / "outputs/TikTokBatchMVP/VERSION")
        self.assertEqual(WINDOW_TITLE, f"TikTok 下载器 {version_file.read_text('utf-8').strip()}",
                         "标题里的版本必须来自 VERSION，不能在代码里另写一份")

    def test_the_window_is_created_with_that_title(self):
        api = Api()
        with patch("web_app.webview.create_window") as create, patch("web_app.webview.start"):
            start_ui(api)
        self.assertEqual(create.call_args.args[0], WINDOW_TITLE)


class WindowIconTests(unittest.TestCase):
    """窗口图标。

    pywebview 的 icon 参数文档写着 "Supported only on GTK/QT"，Windows 后端
    根本不读它 —— 所以源码运行时会顶着 pythonw.exe 的 Python 图标。
    只能自己挂 webview.start(func=...) 去给真实窗口发 WM_SETICON。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_the_icon_asset_is_a_real_multi_size_ico(self):
        self.assertTrue(ICON_PATH.is_file(),
                        "窗口图标资源必须存在，否则打包和运行时都拿不到")
        data = ICON_PATH.read_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00", "不是合法的 ICO 文件头")
        count = int.from_bytes(data[4:6], "little")
        self.assertGreaterEqual(count, 4,
                                "ICO 要含多个尺寸：任务栏、Alt+Tab、资源管理器各取所需")

    def test_a_missing_icon_file_is_not_fatal(self):
        with patch("web_app.BASE", Path(self.temp.name)):
            self.assertFalse(apply_window_icon(attempts=1),
                             "图标缺失不能让应用起不来")

    def test_no_matching_window_is_not_fatal(self):
        self.assertFalse(apply_window_icon(title="__tiktok_no_such_window__", attempts=1))

    def test_start_ui_installs_the_icon_hook(self):
        api = Api()
        with patch("web_app.webview.create_window"), patch("web_app.webview.start") as start:
            start_ui(api)
        self.assertIs(start.call_args.kwargs.get("func"), apply_window_icon,
                      "必须在 webview.start(func=...) 里设置图标，否则 Windows 上没人管")


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
