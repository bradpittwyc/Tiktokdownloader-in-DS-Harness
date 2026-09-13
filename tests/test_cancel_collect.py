"""「暂停抓取」的真实链路测试。

用真实 Chrome + 本机 HTTP 服务，不发任何 TikTok 请求。
验证：取消后 _collect_videos 要干净返回、保住已抓数据、不当成失败。
"""

import http.server
import json
import os
from pathlib import Path
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from web_app import Api  # noqa: E402

PAGE = (b"<!doctype html><meta charset=utf-8><title>probe</title><body>"
        b'<span data-e2e="followers-count">1.2M</span>'
        + b"".join(b'<div data-e2e="user-post-item"><a href="/@probe/video/%d">V%d</a></div>'
                   % (i, i) for i in range(1000, 1040))
        + b"</body>")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(PAGE)))
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args):
        pass


class Server(socketserver.TCPServer):
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Chrome 被强杀时连接会重置，这不是测试失败，别刷一堆堆栈
        pass


class CancelCollectTests(unittest.TestCase):
    def setUp(self):
        # Chrome 有时还占着 profile 目录，清理失败不该让测试变红
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()
        self.api._cookie_browser = ""

    def tearDown(self):
        if self.api._collect_process is not None:
            self.api._stop_process(self.api._collect_process, timeout=10)
        time.sleep(1)
        self.env.stop()
        self.temp.cleanup()

    def test_cancel_sets_the_flag_and_reports_ok(self):
        self.assertFalse(self.api._collect_cancel.is_set())
        self.assertEqual(self.api.cancel_collect(), {"ok": True})
        self.assertTrue(self.api._collect_cancel.is_set())

    def test_cancel_without_a_browser_does_not_spawn_a_thread(self):
        with patch.object(self.api, "_stop_process") as stop:
            self.api.cancel_collect()
            time.sleep(.1)
        stop.assert_not_called()

    def test_cancelling_a_running_collection_stops_it_cleanly(self):
        server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        outcome = {}

        def collect():
            started = time.monotonic()
            try:
                outcome["videos"] = self.api._collect_videos(
                    f"http://127.0.0.1:{server.server_address[1]}/@probe")
                outcome["raised"] = None
            except Exception as exc:
                outcome["raised"] = f"{type(exc).__name__}: {exc}"
            outcome["elapsed"] = time.monotonic() - started

        worker = threading.Thread(target=collect, daemon=True)
        worker.start()
        # 等它真的抓到东西再取消 —— 否则测的是"取消得快"而不是"取消后保住数据"
        archive = self.api._cache_file("probe")
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            if archive.exists():
                try:
                    if json.loads(archive.read_text(encoding="utf-8")).get("videos"):
                        break
                except Exception:
                    pass
            time.sleep(.3)
        else:
            self.fail("40 秒内没有抓到任何作品，测试前提不成立")
        self.api.cancel_collect()
        worker.join(timeout=30)

        self.assertFalse(worker.is_alive(), "取消后抓取没有在 30 秒内结束")
        self.assertIsNone(outcome.get("raised"), "取消不该以异常收场")
        self.assertTrue(self.api._collection_cancelled)
        self.assertIn("已取消抓取", self.api._collection_warning)
        self.assertLess(outcome.get("elapsed", 99), 20, "取消响应太慢")
        # 已经抓到的必须留下，不能因为取消就丢掉
        self.assertTrue(outcome.get("videos"), "取消后已抓到的作品被丢掉了")
        for video in outcome["videos"]:
            self.assertIn("id", video)

    def test_a_fresh_run_clears_the_previous_cancel(self):
        self.api.cancel_collect()
        self.assertTrue(self.api._collect_cancel.is_set())
        with patch.object(self.api, "_launch_native_chrome",
                          side_effect=RuntimeError("stop here")):
            with self.assertRaises(RuntimeError):
                self.api._collect_videos("https://www.tiktok.com/@probe")
        self.assertFalse(self.api._collect_cancel.is_set(),
                         "上一次的取消不能污染下一次抓取")


if __name__ == "__main__":
    unittest.main()
