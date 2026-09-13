"""追新的语义：只抓最新，追上已知历史就停，且不会把历史错误标记为完整。

用真实 Chrome + 本机 HTTP 服务，不发任何 TikTok 请求。
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

IDS = list(range(1000, 1040))

PAGE = (b"<!doctype html><meta charset=utf-8><title>probe</title><body>"
        b'<span data-e2e="followers-count">1.2M</span>'
        + b"".join(b'<div data-e2e="user-post-item"><a href="/@probe/video/%d">V%d</a></div>'
                   % (i, i) for i in IDS)
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
        pass


class FollowNewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()
        self.api._cookie_browser = ""

        self.server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/@probe"

    def tearDown(self):
        if self.api._collect_process is not None:
            self.api._stop_process(self.api._collect_process, timeout=10)
        time.sleep(1)
        self.env.stop()
        self.temp.cleanup()

    def seed_archive(self, ids, complete):
        self.api._store_profile_archive(
            "probe", [{"id": str(i), "url": f"https://www.tiktok.com/@probe/video/{i}",
                       "title": f"旧作品 {i}", "type": "video"} for i in ids], complete)

    def collect(self, newest_only):
        started = time.monotonic()
        videos = self.api._collect_videos(self.url, newest_only=newest_only)
        return videos, time.monotonic() - started

    def test_follow_new_stops_at_known_history(self):
        self.seed_archive([1030], complete=False)
        videos, elapsed = self.collect(newest_only=True)
        self.assertEqual(len(videos), len(IDS), "第一页就该全部拿到")
        self.assertIn("追新完成", self.api._collection_warning)
        self.assertLess(elapsed, 40, "追上已知历史就该立刻停，不该继续翻页到停滞")

    def test_follow_new_never_marks_an_incomplete_history_as_complete(self):
        # 这是最关键的不变量：追新只证明"追上了已知历史"，
        # 若在这里把 complete 置真，history_complete 会被永久错误锁存（B1）
        self.seed_archive([1030], complete=False)
        self.collect(newest_only=True)
        self.assertFalse(self.api._collection_complete,
                         "追新不能把不完整的归档标成完整")
        archive = self.api._load_profile_archive("probe")
        # _collect_videos 自己不落盘，这里只断言内存标志；落盘由调用方负责
        self.assertFalse(self.api._collection_complete)

    def test_a_complete_archive_stays_complete_after_follow_new(self):
        self.seed_archive([1030], complete=True)
        self.collect(newest_only=True)
        # 归档本来就是完整的，增量的老路径仍然认为读取完整
        self.assertTrue(self.api._collection_complete)

    def test_plain_collect_without_the_flag_does_not_short_circuit(self):
        # 对照：不开追新时，不完整的归档不该因为一次撞上已知 id 就判定完成
        self.seed_archive([1030], complete=False)
        # 只跑到"第一轮之后"就取消，避免等 45 秒停滞
        def cancel_soon():
            time.sleep(6)
            self.api.cancel_collect()
        threading.Thread(target=cancel_soon, daemon=True).start()
        self.collect(newest_only=False)
        self.assertFalse(self.api._collection_complete,
                         "普通抓取撞上已知 id 不能算完整")


if __name__ == "__main__":
    unittest.main()
