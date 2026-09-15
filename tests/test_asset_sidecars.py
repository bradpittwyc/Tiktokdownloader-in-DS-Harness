"""素材落盘：封面 / 音频 / 原始信息 / 素材元数据。

这组测试盯的是三类**静默出错**，它们的共同点是"程序不报错，但结果是错的"：

1. 封面是 .jpg、元数据是 .json，正好撞上 MEDIA_EXTS 与 SUBTITLE_EXTS ——
   没下载过的作品会被判成已下载而跳过，元数据会被当成字幕。
2. yt-dlp 的 info 里带着 cookies（实测连匿名提取都有 ttwid），
   原样落盘就是把登录凭证写进用户的视频文件夹。
3. enrich() 曾经用 4 个键整文件覆盖归档，把 history_complete 之类抹掉。

全部离线：不发任何网络请求。
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))
from web_app import (  # noqa: E402
    Api,
    ASSET_SCHEMA,
    build_asset_meta,
    extract_hashtags,
    is_sidecar,
    sanitize_info,
)


# 一份形状跟真实提取结果一致的 info，含真实的凭证字段。
REAL_SHAPED_INFO = {
    "id": "7682095989578091790",
    "title": "As Summer winds down #NASA #RomanSpaceTelescope",
    "description": "As Summer winds down #NASA #RomanSpaceTelescope 🔭",
    "uploader": "nasa",
    "uploader_id": "7664638705177150477",
    "uploader_url": "https://www.tiktok.com/@nasa",
    "channel": "NASA",
    "timestamp": 1788627378,
    "upload_date": "20260905",
    "duration": 101,
    "width": 1080,
    "height": 1920,
    "resolution": "1080x1920",
    "aspect_ratio": 0.5625,
    "format_id": "bytevc1_1080p_561491-1",
    "ext": "mp4",
    "vcodec": "h265",
    "acodec": "aac",
    "view_count": 319800,
    "like_count": 25200,
    "comment_count": 757,
    "repost_count": 1014,
    "track": "original sound - NASA",
    "artist": "NASA",
    "extractor_key": "TikTok",
    "webpage_url": "https://www.tiktok.com/@nasa/video/7682095989578091790",
    "thumbnail": "https://p16-sign.tiktokcdn.com/cover.jpg",
    # 下面两个是真实的凭证字段，任何落盘路径都必须丢掉
    "cookies": "ttwid=1%7COEp_3ruJSA; tt_csrf=abc; sessionid=SECRET",
    "http_headers": {"User-Agent": "Mozilla/5.0", "Cookie": "sessionid=SECRET"},
    "formats": [
        {"format_id": "audio", "vcodec": "none", "acodec": "mp3", "ext": "mp3",
         "http_headers": {"Cookie": "sessionid=SECRET"}},
        {"format_id": "bytevc1_1080p_561491-1", "vcodec": "h265", "acodec": "aac",
         "width": 1080, "height": 1920, "ext": "mp4"},
    ],
}


class SanitizeInfoTests(unittest.TestCase):
    """原始信息存档不能带出登录凭证。"""

    def test_cookies_are_dropped(self):
        cleaned = sanitize_info(REAL_SHAPED_INFO)
        self.assertNotIn("cookies", cleaned)
        self.assertNotIn("http_headers", cleaned)
        self.assertNotIn("sessionid", json.dumps(cleaned),
                         "任何一层都不能残留 sessionid")

    def test_nested_format_headers_are_dropped_too(self):
        # 每个 format 自带一份 http_headers，只清理顶层是不够的
        cleaned = sanitize_info(REAL_SHAPED_INFO)
        for entry in cleaned["formats"]:
            self.assertNotIn("http_headers", entry)
            self.assertNotIn("cookies", entry)

    def test_everything_else_survives(self):
        cleaned = sanitize_info(REAL_SHAPED_INFO)
        self.assertEqual(cleaned["id"], REAL_SHAPED_INFO["id"])
        self.assertEqual(cleaned["view_count"], 319800)
        self.assertEqual(len(cleaned["formats"]), 2)

    def test_it_does_not_mutate_the_original(self):
        sanitize_info(REAL_SHAPED_INFO)
        self.assertIn("cookies", REAL_SHAPED_INFO,
                      "净化必须是复制，不能改到调用方手上那份 info")


class HashtagTests(unittest.TestCase):
    """TikTok 没有 tags 字段，话题只能从文案切。"""

    def test_plain_hashtags(self):
        self.assertEqual(extract_hashtags("hello #NASA #space"), ["NASA", "space"])

    def test_case_insensitive_dedupe_keeps_first_spelling(self):
        self.assertEqual(extract_hashtags("#NASA #nasa #Nasa"), ["NASA"])

    def test_chinese_and_japanese_hashtags(self):
        self.assertEqual(extract_hashtags("今天 #日常 分享 #东京vlog"),
                         ["日常", "东京vlog"])

    def test_a_bare_hash_is_not_a_tag(self):
        self.assertEqual(extract_hashtags("C# 和 # 都不是话题"), [])

    def test_empty_input(self):
        self.assertEqual(extract_hashtags(""), [])
        self.assertEqual(extract_hashtags(None), [])


class IsSidecarTests(unittest.TestCase):
    def test_our_own_files_are_sidecars(self):
        for name in ("a.cover.jpg", "a.info.json", "a.meta.json", "a.audio.mp3"):
            self.assertTrue(is_sidecar(name), name)

    def test_real_media_and_subtitles_are_not(self):
        for name in ("a.mp4", "a.eng-US.vtt", "a.srt", "01.jpg"):
            self.assertFalse(is_sidecar(name), name)


class BuildAssetMetaTests(unittest.TestCase):
    def test_it_carries_the_schema_version(self):
        self.assertEqual(build_asset_meta(REAL_SHAPED_INFO)["schema"], ASSET_SCHEMA)

    def test_it_maps_the_fields_the_library_needs(self):
        meta = build_asset_meta(REAL_SHAPED_INFO)
        self.assertEqual(meta["id"], "7682095989578091790")
        self.assertEqual(meta["author"]["username"], "nasa")
        self.assertEqual(meta["author"]["nickname"], "NASA")
        self.assertEqual(meta["stats"]["views"], 319800)
        self.assertEqual(meta["stats"]["shares"], 1014)
        self.assertEqual(meta["video"]["resolution"], "1080x1920")
        self.assertEqual(meta["music"]["title"], "original sound - NASA")
        self.assertEqual(meta["hashtags"], ["NASA", "RomanSpaceTelescope"])

    def test_info_wins_over_the_scraped_item(self):
        meta = build_asset_meta(REAL_SHAPED_INFO, {"id": "7682095989578091790",
                                                  "title": "旧的抓取标题", "views": 1})
        self.assertEqual(meta["title"], REAL_SHAPED_INFO["title"])
        self.assertEqual(meta["stats"]["views"], 319800)

    def test_it_falls_back_to_the_item_when_info_is_missing(self):
        # 图文帖那条路径根本没有 info，只能靠抓取阶段的 item
        meta = build_asset_meta(None, {"id": "123", "url": "https://x/photo/123",
                                       "title": "图文", "type": "image",
                                       "views": 5, "upload_date": "20260101"})
        self.assertEqual(meta["id"], "123")
        self.assertEqual(meta["type"], "image")
        self.assertEqual(meta["stats"]["views"], 5)
        self.assertEqual(meta["upload_date"], "20260101")

    def test_resolution_is_derived_when_info_omits_it(self):
        meta = build_asset_meta({"width": 720, "height": 1280})
        self.assertEqual(meta["video"]["resolution"], "720x1280")

    def test_file_references_are_bare_names(self):
        meta = build_asset_meta(REAL_SHAPED_INFO, None,
                                {"media": "clip.mp4", "cover": "clip.cover.jpg"})
        self.assertEqual(meta["files"]["media"], "clip.mp4")
        self.assertNotIn("/", meta["files"]["media"])

    def test_zero_counts_are_kept_not_treated_as_missing(self):
        # 0 是合法数据（新作品就是 0 赞），不能因为"看起来假"就丢掉
        meta = build_asset_meta({"view_count": 0, "like_count": 0})
        self.assertEqual(meta["stats"]["views"], 0)
        self.assertEqual(meta["stats"]["likes"], 0)


class FileClassificationTests(unittest.TestCase):
    """回归网：附属文件绝不能被当成媒体或字幕。

    这是本轮最危险的一处 —— 封面一旦被算成"媒体已存在"，下载前的跳过判断
    会把没下载过的作品判成已下载，用户看到"全部完成"却一个文件都没有。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.item = {"id": "7682095989578091790", "title": "Clip One",
                     "upload_date": "20260905"}

    def touch(self, *names):
        for name in names:
            (self.folder / name).write_bytes(b"x")

    def test_a_lone_cover_does_not_count_as_downloaded(self):
        self.touch("Clip One_20260905.cover.jpg")
        media, subtitles = self.api._files_for_item(self.folder, self.item["id"], self.item)
        self.assertEqual(media, [], "只有封面时必须判定为还没下载")
        self.assertEqual(subtitles, [])

    def test_a_lone_meta_file_does_not_count_as_a_subtitle(self):
        self.touch("Clip One_20260905.meta.json", "Clip One_20260905.info.json")
        media, subtitles = self.api._files_for_item(self.folder, self.item["id"], self.item)
        self.assertEqual(media, [])
        self.assertEqual(subtitles, [], "元数据不能被当成字幕")

    def test_a_full_asset_set_yields_exactly_the_media_and_the_subtitle(self):
        self.touch("Clip One_20260905.mp4", "Clip One_20260905.cover.jpg",
                   "Clip One_20260905.info.json", "Clip One_20260905.meta.json",
                   "Clip One_20260905.audio.mp3", "Clip One_20260905.eng-US.vtt")
        media, subtitles = self.api._files_for_item(self.folder, self.item["id"], self.item)
        self.assertEqual([path.name for path in media], ["Clip One_20260905.mp4"])
        self.assertEqual([path.name for path in subtitles], ["Clip One_20260905.eng-US.vtt"])

    def test_the_video_sorts_before_a_same_named_image(self):
        # media[0] 会被下游当成这条作品的媒体文件，顺序不能靠 iterdir
        self.touch("Clip One_20260905.cover.jpg", "Clip One_20260905.mp4")
        media, _ = self.api._files_for_item(self.folder, self.item["id"], self.item)
        self.assertTrue(media[0].name.endswith(".mp4"), media)

    def test_subtitle_siblings_ignore_sidecars(self):
        self.touch("Clip One_20260905.mp4", "Clip One_20260905.info.json",
                   "Clip One_20260905.meta.json", "Clip One_20260905.en.vtt")
        siblings = self.api._subtitle_siblings(self.folder / "Clip One_20260905.mp4",
                                               self.folder)
        self.assertEqual([path.name for path in siblings], ["Clip One_20260905.en.vtt"])


class AudioFormatTests(unittest.TestCase):
    def test_it_picks_the_audio_only_entry(self):
        self.assertEqual(Api._audio_format_id(REAL_SHAPED_INFO), "audio")

    def test_it_returns_none_when_there_is_no_audio_only_format(self):
        self.assertIsNone(Api._audio_format_id(
            {"formats": [{"format_id": "x", "vcodec": "h264", "acodec": "aac"}]}))
        self.assertIsNone(Api._audio_format_id({}))


class SidecarWritingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.folder = Path(self.temp.name) / "@nasa"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.media = self.folder / "Clip One_20260905.mp4"
        self.media.write_bytes(b"video")
        self.item = {"id": "7682095989578091790", "url": "https://www.tiktok.com/@nasa/video/1",
                     "title": "Clip One", "upload_date": "20260905"}

    def write(self, info=REAL_SHAPED_INFO, subtitles=(), audio=None):
        return self.api._write_asset_sidecars(
            self.folder, self.media, info, self.item, list(subtitles), audio)

    def test_it_writes_the_three_sidecars(self):
        written = self.write(subtitles=[self.folder / "Clip One_20260905.en.vtt"])
        self.assertTrue(written["meta"].is_file())
        self.assertTrue(written["info"].is_file())
        self.assertEqual(written["meta"].name, "Clip One_20260905.meta.json")
        self.assertEqual(written["info"].name, "Clip One_20260905.info.json")

    def test_the_written_info_json_carries_no_credentials(self):
        self.write()
        blob = (self.folder / "Clip One_20260905.info.json").read_text(encoding="utf-8")
        self.assertNotIn("sessionid", blob)
        self.assertNotIn("cookies", blob)
        self.assertNotIn("http_headers", blob)
        self.assertIn("7682095989578091790", blob, "该留的字段还得在")

    def test_the_meta_json_lists_the_sibling_files(self):
        written = self.write(subtitles=[self.folder / "Clip One_20260905.en.vtt"])
        meta = json.loads(written["meta"].read_text(encoding="utf-8"))
        self.assertEqual(meta["files"]["media"], "Clip One_20260905.mp4")
        self.assertEqual(meta["files"]["subtitles"], ["Clip One_20260905.en.vtt"])
        self.assertEqual(meta["files"]["info"], "Clip One_20260905.info.json")

    def test_a_failing_cover_does_not_lose_the_other_sidecars(self):
        with patch.object(Api, "_fetch_cover", side_effect=RuntimeError("网络炸了")):
            written = self.write()
        self.assertIsNone(written["cover"])
        self.assertTrue(written["meta"].is_file(), "封面失败不能让元数据也没了")
        self.assertTrue(written["info"].is_file())

    def test_a_missing_thumbnail_is_not_an_error(self):
        written = self.write(info={"id": "1"})
        self.assertIsNone(written["cover"])
        self.assertTrue(written["meta"].is_file())


class EnrichKeepsArchiveFieldsTests(unittest.TestCase):
    """回归网：enrich() 不能再把归档里 videos 以外的键抹掉。

    实测症状：nasdaily.json、primemovies.json 里 schema / history_complete /
    last_sync / count 全没了 —— 因为 enrich 曾经直接写 4 个键的 dict。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.api._emit = lambda function, value: None
        self.cache = self.api._cache_file("nasa")
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        self.cache.write_text(json.dumps({
            "schema": 2, "avatar": "https://cdn/avatar.jpg", "avatar_owner": "nasa",
            "profile_stats": {"followers": 15},
            "videos": [{"id": "1", "url": "https://www.tiktok.com/@nasa/video/1",
                        "title": "old", "upload_date": "20260905"}],
            "history_complete": True, "last_sync": 1700000000, "count": 1,
        }, ensure_ascii=False), encoding="utf-8")

    def run_enrich(self, videos):
        class FakeYDL:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def extract_info(self_inner, url, download=False):
                return {"description": "new title", "like_count": 7, "view_count": 9,
                        "comment_count": 1, "upload_date": "20260905",
                        "thumbnail": "https://cdn/cover.jpg"}

        with patch.object(self.api, "_youtube_dl", return_value=FakeYDL()), \
                patch.object(self.api, "_apply_cookie_options"), \
                patch("time.sleep"):
            self.api.enrich(videos)
        return json.loads(self.cache.read_text(encoding="utf-8"))

    def test_history_complete_survives(self):
        payload = self.run_enrich([{"id": "1", "url": "https://www.tiktok.com/@nasa/video/1",
                                    "title": "old", "upload_date": "20260905"}])
        self.assertTrue(payload.get("history_complete"),
                        "history_complete 被 enrich 抹掉了 —— 这正是要防的回归")

    def test_schema_sync_and_count_survive(self):
        payload = self.run_enrich([{"id": "1", "url": "https://www.tiktok.com/@nasa/video/1",
                                    "title": "old", "upload_date": "20260905"}])
        self.assertEqual(payload.get("schema"), 2)
        # last_sync 的值**应该**被刷新 —— 它语义是"最近一次碰这个博主的时间"，
        # enrich 确实碰了。要守的是这个键本身不能消失（旧代码的 4 键 dict 里没有它）。
        self.assertIsInstance(payload.get("last_sync"), int,
                              "last_sync 被抹掉了：说明没走归档写入器")
        self.assertGreaterEqual(payload["last_sync"], 1700000000)
        self.assertEqual(payload.get("count"), 1)

    def test_avatar_and_profile_stats_survive(self):
        payload = self.run_enrich([{"id": "1", "url": "https://www.tiktok.com/@nasa/video/1",
                                    "title": "old", "upload_date": "20260905"}])
        self.assertEqual(payload.get("avatar"), "https://cdn/avatar.jpg")
        self.assertEqual(payload.get("profile_stats"), {"followers": 15})

    def test_the_enriched_fields_are_actually_written(self):
        payload = self.run_enrich([{"id": "1", "url": "https://www.tiktok.com/@nasa/video/1",
                                    "title": "old", "upload_date": "20260905"}])
        row = payload["videos"][0]
        self.assertEqual(row["title"], "new title")
        self.assertEqual(row["likes"], 7)
        self.assertEqual(row["cover"], "https://cdn/cover.jpg")


class FakeDownloader:
    """只写文件 + 回调 progress_hooks，完全不发网络请求。"""

    def __init__(self, options, writer):
        self.options = options
        self.writer = writer

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return self.writer(self.options)

    def download(self, urls):
        # 模拟 yt-dlp 按 outtmpl 落盘（音频那次调用走这里）
        name = self.options["outtmpl"].replace("%(ext)s", "mp3")
        Path(name).write_bytes(b"audio")
        # 故意再写一个同前缀的字幕。真实的第二次调用曾经因为继承了
        # writesubtitles 而下回一份 "<名>.audio.eng-US.vtt"，
        # 而 "eng-US.vtt" 按字母排在 "mp3" 前面，把 files.audio 顶掉了。
        # 这里无条件写，用来验证扩展名过滤这一层兜得住。
        Path(self.options["outtmpl"].replace("%(ext)s", "eng-US.vtt")).write_bytes(b"WEBVTT")


class DownloadFallbackTests(unittest.TestCase):
    """回归网：progress_hooks 报出来的文件里混着字幕。

    **实测踩到的 bug**：标题里的 emoji 让 _files_for_item 的模糊匹配落空，
    代码走了"信任 yt-dlp 报告的文件"那条兜底 —— 但 progress_hooks 对**每一个**
    下载的文件都会触发，字幕也在里面，而且字幕先下完。于是 media_files[0]
    是个 .vtt，封面/音频/元数据全挂到了字幕的前缀上：

        <名>_20260905.eng-US.cover.jpg
        <名>_20260905.eng-US.meta.json      <- files.media 还写成那个 .vtt

    这个只有真跑一次下载才看得见，单元测试和类型检查都不会报。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.api._emit = lambda function, value: None
        self.out = Path(self.temp.name) / "out"
        self.downloaders = []

    def run_download(self, item, produced):
        """produced: [(文件名, 内容)]，顺序 = progress_hooks 的触发顺序。"""
        def writer(options):
            folder = Path(options["outtmpl"]).parent
            folder.mkdir(parents=True, exist_ok=True)
            for name, blob in produced:
                path = folder / name
                path.write_bytes(blob)
                for hook in options.get("progress_hooks") or []:
                    hook({"status": "finished", "filename": str(path)})
            return dict(REAL_SHAPED_INFO)

        def fake_cover(url, output, referer=""):
            Path(output).write_bytes(b"jpg-bytes")
            return Path(output)

        def build(options):
            downloader = FakeDownloader(options, writer)
            self.downloaders.append(downloader)
            return downloader

        with patch.object(self.api, "_youtube_dl", side_effect=build), \
                patch.object(Api, "_fetch_cover", staticmethod(fake_cover)), \
                patch.object(self.api, "_apply_cookie_options"):
            return self.api.download([item], str(self.out), "best", retry_count=1, concurrency=1)

    def item(self):
        # 标题故意跟真实文件名对不上，逼出兜底分支
        return {"id": "7682095989578091790",
                "url": "https://www.tiktok.com/@nasa/video/7682095989578091790",
                "title": "标题跟文件名完全不一样", "upload_date": "20260905",
                "type": "video", "cover": ""}

    def test_the_media_is_the_video_even_when_the_subtitle_finishes_first(self):
        result = self.run_download(self.item(), [
            ("Real Clip_20260905.eng-US.vtt", b"WEBVTT\n"),  # 字幕先完成 —— 实测就是这个顺序
            ("Real Clip_20260905.mp4", b"video"),
        ])
        self.assertEqual(result["ok"], 1, result)
        folder = Path(result["folder"])
        meta = json.loads((folder / "Real Clip_20260905.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["files"]["media"], "Real Clip_20260905.mp4",
                         "媒体取成字幕了：兜底没过滤 progress_hooks 里的字幕")

    def test_the_sidecars_are_named_after_the_video(self):
        result = self.run_download(self.item(), [
            ("Real Clip_20260905.eng-US.vtt", b"WEBVTT\n"),
            ("Real Clip_20260905.mp4", b"video"),
        ])
        folder = Path(result["folder"])
        for suffix in (".cover.jpg", ".info.json", ".meta.json", ".audio.mp3"):
            self.assertTrue((folder / f"Real Clip_20260905{suffix}").is_file(),
                            f"缺少 Real Clip_20260905{suffix}；目录里是 "
                            f"{sorted(p.name for p in folder.iterdir())}")

    def test_the_audio_is_recorded_in_the_meta(self):
        result = self.run_download(self.item(), [("Real Clip_20260905.mp4", b"video")])
        folder = Path(result["folder"])
        meta = json.loads((folder / "Real Clip_20260905.meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["files"]["audio"], "Real Clip_20260905.audio.mp3",
                         "音频没落成 .audio.mp3（文件名模板没钉死）")

    def test_the_audio_run_does_not_write_subtitles(self):
        """音频那一次 yt-dlp 不能再去下字幕。

        实测踩到：它继承了 writesubtitles=True，于是又落了一份
        "<名>.audio.eng-US.vtt"，被当成音频轨记进了 meta。
        """
        self.run_download(self.item(), [("Real Clip_20260905.mp4", b"video")])
        audio_runs = [downloader for downloader in self.downloaders
                      if str(downloader.options.get("format")) == "audio"]
        self.assertTrue(audio_runs, "应该有一次专门的音频下载")
        for downloader in audio_runs:
            self.assertFalse(downloader.options.get("writesubtitles"),
                             "音频那次必须显式关掉 writesubtitles")
            self.assertFalse(downloader.options.get("writeautomaticsub"))

    def test_a_failed_audio_download_still_reports_success(self):
        def writer(options):
            folder = Path(options["outtmpl"]).parent
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "Real Clip_20260905.mp4"
            path.write_bytes(b"video")
            for hook in options.get("progress_hooks") or []:
                hook({"status": "finished", "filename": str(path)})
            return dict(REAL_SHAPED_INFO)

        class Exploding(FakeDownloader):
            def download(self, urls):
                raise RuntimeError("音频接口挂了")

        with patch.object(self.api, "_youtube_dl",
                          side_effect=lambda options: Exploding(options, writer)), \
                patch.object(Api, "_fetch_cover", staticmethod(lambda url, output, referer="": None)), \
                patch.object(self.api, "_apply_cookie_options"):
            result = self.api.download([self.item()], str(self.out), "best",
                                       retry_count=1, concurrency=1)

        self.assertEqual(result["ok"], 1, "音频失败不能让视频变成失败")
        folder = Path(result["folder"])
        self.assertTrue((folder / "Real Clip_20260905.meta.json").is_file(),
                        "音频失败不能让元数据也没了")


if __name__ == "__main__":
    unittest.main()
