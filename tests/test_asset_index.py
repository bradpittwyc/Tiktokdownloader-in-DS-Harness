"""素材索引与补齐。

索引层（asset_index.py）是**纯本地**的：只读磁盘、不联网，所以绝大部分
测试可以直接对着临时目录跑，不需要 Api、不需要假 bridge。

补齐（Api.backfill_assets）要联网重新提取信息，这里用假 yt-dlp 打桩，
验证的是"该补的补了、该跳的跳了、能取消、失败不炸"这些行为。
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "outputs/TikTokBatchMVP"))
from asset_index import (  # noqa: E402
    DESC_LIMIT,
    build_asset,
    build_post_asset,
    index_summary,
    is_post_folder,
    library_rows,
    load_meta,
    media_files,
    scan_assets,
    subtitle_siblings,
)
from web_app import Api  # noqa: E402

TITLE = "Clip One"
STAMP = "20260905"


def make(folder, name, blob=b"x"):
    path = Path(folder) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob)
    return path


class MediaDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_sidecars_are_not_media(self):
        make(self.folder, "clip.mp4")
        make(self.folder, "clip.cover.jpg")
        make(self.folder, "clip.audio.mp3")
        make(self.folder, "clip.info.json")
        self.assertEqual([p.name for p in media_files(self.folder)], ["clip.mp4"])

    def test_video_sorts_before_images(self):
        make(self.folder, "clip.cover.jpg")
        make(self.folder, "clip.png")
        make(self.folder, "clip.mp4")
        self.assertEqual(media_files(self.folder)[0].name, "clip.mp4")

    def test_photo_post_images_are_still_media(self):
        # 图文帖的 jpg 没有 .cover 后缀，是真正的媒体
        make(self.folder, "01.jpg")
        make(self.folder, "02.jpg")
        self.assertEqual(len(media_files(self.folder)), 2)

    def test_subtitle_siblings_exclude_sidecars(self):
        make(self.folder, "clip.mp4")
        make(self.folder, "clip.en.vtt")
        make(self.folder, "clip.info.json")
        make(self.folder, "clip.meta.json")
        self.assertEqual([Path(p).name for p in subtitle_siblings(self.folder, "clip")],
                         ["clip.en.vtt"])


class BuildAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_a_fresh_download_that_lost_everything_reports_all_gaps(self):
        media = make(self.folder, f"{TITLE}_{STAMP}.mp4")
        asset = build_asset(self.folder, media)
        self.assertEqual(asset["gaps"], ["cover", "audio", "info", "meta"])

    def test_a_complete_asset_reports_no_gaps(self):
        media = make(self.folder, f"{TITLE}_{STAMP}.mp4")
        make(self.folder, f"{TITLE}_{STAMP}.cover.jpg")
        make(self.folder, f"{TITLE}_{STAMP}.audio.mp3")
        make(self.folder, f"{TITLE}_{STAMP}.info.json")
        make(self.folder, f"{TITLE}_{STAMP}.meta.json")
        self.assertEqual(build_asset(self.folder, media)["gaps"], [])

    def test_empty_files_do_not_count_as_present(self):
        # 0 字节的残留（下到一半断了）不能算补齐了
        media = make(self.folder, f"{TITLE}_{STAMP}.mp4")
        make(self.folder, f"{TITLE}_{STAMP}.cover.jpg", b"")
        make(self.folder, f"{TITLE}_{STAMP}.meta.json", b"")
        self.assertEqual(build_asset(self.folder, media)["gaps"],
                         ["cover", "audio", "info", "meta"])

    def test_paths_are_strings_so_the_index_can_be_sent_as_json(self):
        media = make(self.folder, f"{TITLE}_{STAMP}.mp4")
        make(self.folder, f"{TITLE}_{STAMP}.cover.jpg")
        asset = build_asset(self.folder, media)
        self.assertIsInstance(asset["media"], str)
        self.assertIsInstance(asset["cover"], str)
        self.assertIsInstance(asset["folder"], str)
        json.dumps(asset, ensure_ascii=False)      # 不能抛

    def test_a_missing_subtitle_is_not_a_gap(self):
        # 有的作品本来就没字幕，缺了不代表"没补齐"
        media = make(self.folder, f"{TITLE}_{STAMP}.mp4")
        for suffix in (".cover.jpg", ".audio.mp3", ".info.json", ".meta.json"):
            make(self.folder, f"{TITLE}_{STAMP}{suffix}")
        asset = build_asset(self.folder, media)
        self.assertEqual(asset["subtitles"], [])
        self.assertEqual(asset["gaps"], [])


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_it_finds_assets_in_subfolders(self):
        make(self.root, "@nasa/a_20260101.mp4")
        make(self.root, "@nasa/b_20260102.mp4")
        make(self.root, "@spacex/c_20260103.mp4")
        assets, problems = scan_assets(self.root)
        self.assertEqual(problems, [])
        self.assertEqual(len(assets), 3)

    def test_a_photo_post_folder_is_one_asset_not_one_per_image(self):
        # 这是最容易做错的地方：递归时每张 jpg 都会变成一条素材
        make(self.root, "Title_20260905_[7682095989578091790]/01.jpg")
        make(self.root, "Title_20260905_[7682095989578091790]/02.jpg")
        make(self.root, "Title_20260905_[7682095989578091790]/03.jpg")
        assets, _ = scan_assets(self.root)
        self.assertEqual(len(assets), 1, [a["stem"] for a in assets])
        self.assertEqual(assets[0]["type"], "image")
        self.assertEqual(assets[0]["id"], "7682095989578091790")
        self.assertEqual(len(assets[0]["images"]), 3)

    def test_a_photo_post_only_ever_misses_its_meta(self):
        make(self.root, "Title_20260905_[123]/01.jpg")
        assets, _ = scan_assets(self.root)
        self.assertEqual(assets[0]["gaps"], ["meta"])

    def test_scanning_a_missing_folder_is_reported_not_raised(self):
        assets, problems = scan_assets(self.root / "nope")
        self.assertEqual(assets, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("不存在", problems[0]["error"])

    def test_non_recursive_only_looks_at_the_top_level(self):
        make(self.root, "top_20260101.mp4")
        make(self.root, "@nasa/deep_20260102.mp4")
        assets, _ = scan_assets(self.root, recursive=False)
        self.assertEqual([a["stem"] for a in assets], ["top_20260101"])

    def test_post_folder_detection(self):
        self.assertTrue(is_post_folder(self.root / "标题_20260905_[12345]"))
        self.assertFalse(is_post_folder(self.root / "@nasa"))
        self.assertFalse(is_post_folder(self.root / "标题_20260905"))


class SummaryTests(unittest.TestCase):
    def test_it_counts_each_gap_kind(self):
        assets = [{"gaps": ["cover", "meta"], "type": "video", "media": None},
                  {"gaps": [], "type": "video", "media": None},
                  {"gaps": ["meta"], "type": "image", "media": None}]
        summary = index_summary(assets)
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["complete"], 1)
        self.assertEqual(summary["incomplete"], 2)
        self.assertEqual(summary["byGap"]["meta"], 2)
        self.assertEqual(summary["byGap"]["cover"], 1)
        self.assertEqual(summary["videos"], 2)
        self.assertEqual(summary["posts"], 1)

    def test_total_bytes_survives_missing_paths(self):
        summary = index_summary([{"gaps": [], "type": "video",
                                  "media": "Z:/definitely/not/here.mp4"}])
        self.assertEqual(summary["bytes"], 0)


class LoadMetaTests(unittest.TestCase):
    def test_broken_json_returns_none_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as folder:
            path = make(folder, "x.meta.json", b"{not json")
            self.assertIsNone(load_meta(path))

    def test_a_json_array_is_not_a_meta(self):
        with tempfile.TemporaryDirectory() as folder:
            path = make(folder, "x.meta.json", b"[1,2,3]")
            self.assertIsNone(load_meta(path))


class FakeInfoYDL:
    def __init__(self, options, info):
        self.options = options
        self.info = info

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return dict(self.info)


class BackfillTests(unittest.TestCase):
    """补齐：给已经下载过的作品补附属文件。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.events = []
        self.api._emit = lambda function, value: self.events.append((function, value))
        self.root = Path(self.temp.name) / "downloads"
        self.target = self.root / "@owner"
        self.target.mkdir(parents=True, exist_ok=True)
        self.media = make(self.target, f"{TITLE}_{STAMP}.mp4")
        self.write_archive([{"id": "111", "url": "https://www.tiktok.com/@owner/video/111",
                             "title": TITLE, "upload_date": STAMP, "type": "video"}])

    def write_archive(self, videos):
        path = self.api._cache_file("owner")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 2, "videos": videos}, ensure_ascii=False),
                        encoding="utf-8")

    def info(self):
        return {"id": "111", "webpage_url": "https://www.tiktok.com/@owner/video/111",
                "title": TITLE, "description": f"{TITLE} #tag", "upload_date": STAMP,
                "uploader": "owner", "channel": "Owner", "duration": 12,
                "view_count": 5, "like_count": 1, "thumbnail": "https://cdn/cover.jpg"}

    def run_backfill(self, **kwargs):
        def fake_cover(url, output, referer=""):
            Path(output).write_bytes(b"jpg")
            return Path(output)

        with patch.object(self.api, "_youtube_dl",
                          side_effect=lambda options: FakeInfoYDL(options, self.info())), \
                patch.object(Api, "_fetch_cover", staticmethod(fake_cover)), \
                patch.object(self.api, "_apply_cookie_options"), \
                patch.object(Api, "_audio_format_id", staticmethod(lambda info: None)):
            return self.api.backfill_assets(str(self.root), "owner", **kwargs)

    def test_it_fills_every_gap(self):
        result = self.run_backfill()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["updated"], 1)
        for suffix in (".cover.jpg", ".info.json", ".meta.json"):
            self.assertTrue((self.target / f"{TITLE}_{STAMP}{suffix}").is_file(), suffix)
        self.assertEqual(build_asset(self.target, self.media)["gaps"], ["audio"],
                         "音频这一轮被桩掉了，只剩它该缺")

    def test_the_written_meta_matches_the_download_path_shape(self):
        self.run_backfill()
        meta = load_meta(self.target / f"{TITLE}_{STAMP}.meta.json")
        self.assertEqual(meta["schema"], 1)
        self.assertEqual(meta["hashtags"], ["tag"])
        self.assertEqual(meta["files"]["media"], f"{TITLE}_{STAMP}.mp4")
        self.assertEqual(meta["files"]["cover"], f"{TITLE}_{STAMP}.cover.jpg")

    def test_an_already_complete_asset_is_skipped(self):
        for suffix in (".cover.jpg", ".audio.mp3", ".info.json", ".meta.json"):
            make(self.target, f"{TITLE}_{STAMP}{suffix}")
        result = self.run_backfill()
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["examined"], 0, "已经齐了就不该进补齐流程")

    def test_a_video_with_no_local_file_is_ignored(self):
        self.write_archive([{"id": "999", "url": "https://www.tiktok.com/@owner/video/999",
                             "title": "根本没下过", "upload_date": "20200101"}])
        result = self.run_backfill()
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["failed"], [])

    def test_it_refuses_when_there_is_no_archive(self):
        # 没有归档就不知道作品链接，只能如实说补不了
        self.api._cache_file("owner").unlink()
        result = self.run_backfill()
        self.assertFalse(result["ok"])
        self.assertIn("归档", result["error"])

    def test_it_refuses_without_a_folder_or_username(self):
        self.assertFalse(self.api.backfill_assets("", "owner")["ok"])
        self.assertFalse(self.api.backfill_assets(str(self.root), "")["ok"])

    def test_progress_events_always_end(self):
        # 进度提示必须有终点，否则界面上会一直挂着"补齐中"
        self.run_backfill()
        names = [value for function, value in self.events if function == "assetBackfill"]
        self.assertTrue(names, "应该有 assetBackfill 事件")
        self.assertTrue(any(entry.get("done") for entry in names), "缺少收尾事件")

    def test_a_extraction_failure_still_writes_a_meta_from_the_archive(self):
        class Exploding(FakeInfoYDL):
            def extract_info(self, url, download=False):
                raise RuntimeError("TikTok 挂了")

        with patch.object(self.api, "_youtube_dl",
                          side_effect=lambda options: Exploding(options, {})), \
                patch.object(Api, "_fetch_cover", staticmethod(lambda url, output, referer="": None)), \
                patch.object(self.api, "_apply_cookie_options"), \
                patch.object(Api, "_audio_format_id", staticmethod(lambda info: None)):
            result = self.api.backfill_assets(str(self.root), "owner")
        self.assertEqual(result["updated"], 1, "提不到信息也该用归档数据写出 meta")
        meta = load_meta(self.target / f"{TITLE}_{STAMP}.meta.json")
        self.assertEqual(meta["id"], "111")
        self.assertEqual(meta["title"], TITLE)

    def test_cancelling_stops_early(self):
        self.write_archive([{"id": str(i), "url": f"https://www.tiktok.com/@owner/video/{i}",
                             "title": TITLE, "upload_date": STAMP} for i in range(1, 6)])
        api = self.api
        original = api._files_for_item
        calls = []

        def counting(folder, item_id, item=None):
            calls.append(item_id)
            api._asset_cancel.set() if len(calls) >= 2 else None
            return original(folder, item_id, item)

        with patch.object(api, "_files_for_item", side_effect=counting):
            result = self.run_backfill()
        self.assertTrue(result["cancelled"])
        self.assertLess(len(calls), 5, "取消之后不该继续走完全部作品")


class LibraryRowTests(unittest.TestCase):
    """素材行：把磁盘实际情况 + meta.json 压成前端检索用的扁平记录。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.media = make(self.folder, f"{TITLE}_{STAMP}.mp4")

    def asset_with(self, meta):
        if meta is not None:
            make(self.folder, f"{TITLE}_{STAMP}.meta.json",
                 json.dumps(meta, ensure_ascii=False).encode("utf-8"))
        return build_asset(self.folder, self.media)

    def test_a_missing_meta_still_produces_a_usable_row(self):
        # 还没补齐的素材也必须能出现在列表里，否则用户根本看不到它们
        rows = library_rows([self.asset_with(None)])
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["hasMeta"])
        self.assertEqual(rows[0]["title"], f"{TITLE}_{STAMP}")   # 退回文件名
        self.assertEqual(rows[0]["gaps"], ["cover", "audio", "info", "meta"])

    def test_it_flattens_the_nested_meta(self):
        asset = self.asset_with({
            "schema": 1, "id": "111", "title": "Moon landing", "upload_date": "20260905",
            "duration": 66, "hashtags": ["Moon", "Artemis"],
            "description": "summer winds down",
            "author": {"username": "nasa", "nickname": "NASA"},
            "stats": {"views": 341100, "likes": 31900, "comments": 12, "shares": 30},
            "video": {"resolution": "720x1280"},
        })
        row = library_rows([asset])[0]
        self.assertTrue(row["hasMeta"])
        self.assertEqual(row["id"], "111")
        self.assertEqual(row["author"], "nasa")
        self.assertEqual(row["nickname"], "NASA")
        self.assertEqual(row["views"], 341100)
        self.assertEqual(row["likes"], 31900)
        self.assertEqual(row["resolution"], "720x1280")
        self.assertEqual(row["hashtags"], ["Moon", "Artemis"])

    def test_a_broken_meta_falls_back_instead_of_raising(self):
        make(self.folder, f"{TITLE}_{STAMP}.meta.json", b"{not json")
        rows = library_rows([build_asset(self.folder, self.media)])
        self.assertFalse(rows[0]["hasMeta"])
        self.assertEqual(rows[0]["title"], f"{TITLE}_{STAMP}")

    def test_the_description_is_capped_so_the_payload_stays_small(self):
        asset = self.asset_with({"title": "x", "description": "长" * 5000})
        row = library_rows([asset])[0]
        self.assertEqual(len(row["desc"]), DESC_LIMIT)

    def test_a_photo_post_row_keeps_its_id_and_type(self):
        make(self.folder, "Post_20260905_[333]/01.jpg")
        assets, _ = scan_assets(self.folder)
        # 这个目录里还有 setUp 建的视频，所以按类型挑而不是按下标取
        row = next(r for r in library_rows(assets) if r["type"] == "image")
        self.assertEqual(row["id"], "333")

    def test_the_whole_payload_is_json_serialisable(self):
        make(self.folder, f"{TITLE}_{STAMP}.cover.jpg")
        rows = library_rows([build_asset(self.folder, self.media)])
        json.dumps(rows, ensure_ascii=False)


class ScanApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.root = Path(self.temp.name) / "downloads"

    def test_it_returns_every_asset_not_only_the_incomplete_ones(self):
        # 检索要在整库上做，只给"待补的"会让搜索看起来漏东西
        make(self.root, "@a/one_20260101.mp4")
        make(self.root, "@a/two_20260102.mp4")
        for suffix in (".cover.jpg", ".audio.mp3", ".info.json", ".meta.json"):
            make(self.root, f"@a/two_20260102{suffix}")
        result = self.api.scan_assets(str(self.root))
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["total"], 2)
        self.assertEqual(result["summary"]["complete"], 1)
        stems = sorted(row["stem"] for row in result["rows"])
        self.assertEqual(stems, ["one_20260101", "two_20260102"])
        one = next(row for row in result["rows"] if row["stem"] == "one_20260101")
        self.assertEqual(one["gaps"], ["cover", "audio", "info", "meta"])
        two = next(row for row in result["rows"] if row["stem"] == "two_20260102")
        self.assertEqual(two["gaps"], [])

    def test_it_reads_the_meta_into_the_rows(self):
        make(self.root, "@a/one_20260101.mp4")
        make(self.root, "@a/one_20260101.meta.json", json.dumps(
            {"title": "标题", "author": {"username": "nasa"}, "stats": {"views": 5}},
            ensure_ascii=False).encode("utf-8"))
        row = self.api.scan_assets(str(self.root))["rows"][0]
        self.assertEqual(row["title"], "标题")
        self.assertEqual(row["author"], "nasa")
        self.assertEqual(row["views"], 5)

    def test_the_payload_is_json_serialisable(self):
        make(self.root, "@a/one_20260101.mp4")
        json.dumps(self.api.scan_assets(str(self.root)), ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
