"""下载落盘后的文件识别。

真机现场：文件明明下好了，却报"下载器未生成目标文件"。
原因是 app 侧的 upload_date 按本地时间算、yt-dlp 的 %(upload_date)s 按 UTC，
晚上上传的视频差一天，模糊匹配的日期校验就挂了。

这个文件同时锁住"日期容差"和"以 yt-dlp 报告的确切路径兜底"两条。
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from web_app import Api, upload_date_matches  # noqa: E402

# 真机复现用的原始数据：item 说 20260904，文件名却是 20260903
REAL_TITLE = ("Some things always come first. Some things always come first. "
              "#TheRiverWild #MerylStreep #DavidStrathairn #Movies #MovieClips")


class UploadDateMatchTests(unittest.TestCase):
    def test_exact_date_matches(self):
        self.assertTrue(upload_date_matches("20260903", "x_20260903.mp4"))

    def test_one_day_off_still_matches(self):
        # 本地时间 vs UTC：晚上上传就跨了一天
        self.assertTrue(upload_date_matches("20260904", "x_20260903.mp4"))
        self.assertTrue(upload_date_matches("20260902", "x_20260903.mp4"))

    def test_two_days_off_does_not_match(self):
        self.assertFalse(upload_date_matches("20260905", "x_20260903.mp4"))

    def test_missing_date_matches_anything(self):
        self.assertTrue(upload_date_matches("", "x_anything.mp4"))
        self.assertTrue(upload_date_matches(None, "x_anything.mp4"))

    def test_garbage_date_does_not_crash(self):
        self.assertFalse(upload_date_matches("not-a-date", "x_20260903.mp4"))


class FilesForItemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.api = Api()

    def tearDown(self):
        self.temp.cleanup()

    def make(self, name, size=128):
        path = self.folder / name
        path.write_bytes(b"x" * size)
        return path

    def item(self, **overrides):
        base = {"id": "7681416724331154701", "url": "https://www.tiktok.com/@owner/video/1",
                "title": REAL_TITLE, "upload_date": "20260904", "type": "video"}
        base.update(overrides)
        return base

    def test_the_real_failure_now_resolves(self):
        self.make(REAL_TITLE + "_20260903.mp4")
        self.make(REAL_TITLE + "_20260903.eng-US.vtt")
        media, subtitles = self.api._files_for_item(self.folder, "7681416724331154701", self.item())
        self.assertEqual([p.suffix for p in media], [".mp4"])
        self.assertEqual([p.suffix for p in subtitles], [".vtt"])

    def test_media_is_not_confused_with_subtitles(self):
        self.make(REAL_TITLE + "_20260903.mp4")
        self.make(REAL_TITLE + "_20260903.vtt")
        media, subtitles = self.api._files_for_item(self.folder, "1", self.item())
        self.assertNotIn(".vtt", [p.suffix for p in media])
        self.assertNotIn(".mp4", [p.suffix for p in subtitles])

    def test_id_in_the_filename_wins(self):
        named = self.make("anything_[7681416724331154701].mp4")
        media, _ = self.api._files_for_item(self.folder, "7681416724331154701",
                                            self.item(title="完全不同", upload_date="19990101"))
        self.assertEqual(media, [named])

    def test_a_different_video_with_a_different_title_is_not_matched(self):
        self.make("Totally different clip_20260903.mp4")
        media, _ = self.api._files_for_item(self.folder, "1", self.item())
        self.assertEqual(media, [])

    def test_a_far_off_date_is_not_matched(self):
        self.make(REAL_TITLE + "_20250101.mp4")
        media, _ = self.api._files_for_item(self.folder, "1", self.item())
        self.assertEqual(media, [])

    def test_partial_downloads_are_ignored(self):
        self.make(REAL_TITLE + "_20260903.mp4.part")
        media, _ = self.api._files_for_item(self.folder, "1", self.item())
        self.assertEqual(media, [])

    def test_no_item_means_only_the_id_check(self):
        self.make(REAL_TITLE + "_20260903.mp4")
        media, _ = self.api._files_for_item(self.folder, "1", None)
        self.assertEqual(media, [])

    def test_subtitle_siblings_follow_the_media_name(self):
        media = self.make(REAL_TITLE + "_20260903.mp4")
        self.make(REAL_TITLE + "_20260903.eng-US.vtt")
        self.make(REAL_TITLE + "_20260903.zh-Hans.srt")
        self.make("unrelated.vtt")
        siblings = self.api._subtitle_siblings(media, self.folder)
        self.assertEqual(sorted(p.suffix for p in siblings), [".srt", ".vtt"])
        self.assertEqual(len(siblings), 2)


class FakeYDL:
    """模拟 yt-dlp：写一个模板根本猜不出来的文件名，并回调 progress hook。"""

    def __init__(self, options, path):
        self.options = options
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        self.path.write_bytes(b"fake media")
        for hook in self.options["progress_hooks"]:
            hook({"status": "finished", "filename": str(self.path),
                  "downloaded_bytes": 10, "total_bytes": 10})


class DownloadResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.api = Api()
        self.api._cookie_browser = ""
        self.api._cookie_file = ""
        self.events = []
        self.api._emit = lambda function, value: self.events.append((function, value))

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def item(self, **overrides):
        base = {"id": "7681416724331154701",
                "url": "https://www.tiktok.com/@owner/video/7681416724331154701",
                "title": REAL_TITLE, "upload_date": "20260904", "type": "video"}
        base.update(overrides)
        return base

    def run_download(self, item, written_name):
        folder = Path(self.temp.name) / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "@owner"
        target.mkdir(parents=True, exist_ok=True)
        written = target / written_name
        with patch.object(self.api, "_youtube_dl",
                          side_effect=lambda options: FakeYDL(options, written)):
            return self.api.download([item], str(folder), "best", retry_count=0)

    def test_exact_path_from_ytdlp_rescues_an_unpredictable_name(self):
        # 文件名跟 title/upload_date 完全对不上，模板法必然找不到
        item = self.item()
        result = self.run_download(item, "completely-unexpected-name.mp4")
        self.assertEqual(result["failed"], [], "下好了就不该报'未生成目标文件'")
        self.assertEqual(result["ok"], 1)

    def test_success_reports_the_folder_and_sibling_subtitles(self):
        item = self.item()
        folder = Path(self.temp.name) / "downloads"
        target = folder / "@owner"
        target.mkdir(parents=True, exist_ok=True)
        (target / "odd-name.mp4").write_bytes(b"media")
        (target / "odd-name.eng-US.vtt").write_text("WEBVTT", encoding="utf-8")
        with patch.object(self.api, "_youtube_dl",
                          side_effect=lambda options: FakeYDL(options, target / "odd-name.mp4")):
            self.api.download([item], str(folder), "best", retry_count=0)
        done = [value for function, value in self.events
                if function == "downloadProgress" and value.get("state") == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["folder"], str(target))
        self.assertEqual([Path(p).name for p in done[0]["subtitles"]],
                         ["odd-name.eng-US.vtt"],
                         "兜底找到的媒体，它的同名字幕不能被丢掉")

    def test_a_template_guessable_name_still_works(self):
        item = self.item(upload_date="20260903")
        result = self.run_download(item, REAL_TITLE + "_20260903.mp4")
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["ok"], 1)

    def test_a_genuinely_missing_file_is_still_reported(self):
        item = self.item()
        folder = Path(self.temp.name) / "downloads"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "@owner").mkdir(parents=True, exist_ok=True)

        class SilentYDL(FakeYDL):
            def download(self, urls):
                pass          # 什么都没写，也没回调 hook

        with patch.object(self.api, "_youtube_dl",
                          side_effect=lambda options: SilentYDL(options, folder / "nope.mp4")):
            result = self.api.download([item], str(folder), "best", retry_count=0)
        self.assertEqual(result["ok"], 0)
        self.assertIn("未生成目标文件", result["failed"][0]["error"])


if __name__ == "__main__":
    unittest.main()
