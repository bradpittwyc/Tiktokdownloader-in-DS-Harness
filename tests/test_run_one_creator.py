"""「立即跑 1 条」—— 单条闭环的回归网（Creator Monitor → Collector → Downloader →
Content Library → Transcript / ASR → AI Enrichment）。

为什么单开一份：这一轮修的是「用户不知道怎么让系统真的下 1 条并跑完流程」，
用户入口只有一句「立即跑 1 条」，背后要串起六段已有能力。下面每一条断言都对应
一个用户真的会遇到的后果，而不是实现细节：

- 只跑 1 条：点了按钮不能顺手把整个队列都下载了（这是最容易写错的边界）
- 只跑这位 Creator：绝不能拿别的 Creator 的任务
- 重复内容跳过：「没有新内容」是正常结果，不是报错
- 失败要说清楚在哪一层：下载 / 字幕 / AI / 前置检查

全部离线：下载器、模型、ASR 都是假替身，但**入库仍然走真实链路**
（假下载器像真下载器一样回调 content_register_download），
所以「下载 → 内容库 → 转写 → AI」这一段测的是真代码。
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_factory.ai_enrichment import EnrichmentService  # noqa: E402
from content_factory.collector.bridge_api import CollectorApi  # noqa: E402
from content_factory.collector.sources import StaticCandidateSource  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.pipeline import ContentPipeline  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402

# 与管道最短字幕长度要求一致（太短会被判「标注不出有效内容」）
SUBTITLE_TEXT = "NASA just landed a rover on Mars and it works.\n"
ASR_TEXT = ("NASA just landed a rover on Mars and it works. This fake ASR text is long "
            "enough to pass the minimum transcript length check.")

MODEL_REPLY = json.dumps({
    "topic": "AI 科技", "subtopic": "航天", "cefr_level": "b2", "accent": "美音",
    "speech_speed": "中速", "learning_value": 0.9, "keywords": ["rover", "mars"],
    "expressions": [{"text": "landed a rover on Mars", "meaning_zh": "把探测器送上火星"}],
    "grammar_points": ["现在完成时"],
    "key_sentences": [{"text": "NASA just landed a rover on Mars and it works.",
                       "translation_zh": "NASA 刚刚把探测器送上火星，而且它工作正常。"}],
    "summary_zh": "NASA 火星任务取得进展。", "recommended_task": "复述",
}, ensure_ascii=False)


class FakeResponse:
    def __init__(self, text, status=200):
        self._text, self.status_code = text, status

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


class CountingHttp:
    """假模型：记录调用次数，用它证明「下载失败时根本没走到 AI」。"""

    def __init__(self, reply=MODEL_REPLY):
        self.reply, self.calls = reply, []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(url)
        return FakeResponse(self.reply)


class FakeTranscriber:
    """假的「本机 ASR」：从 ContentPipeline 自己的注入点进去，不是另造一条转写链路。"""

    def __init__(self, text=ASR_TEXT, error=None):
        self.text, self.error, self.calls = text, error, []

    def __call__(self, media, **kwargs):
        self.calls.append(media)
        if self.error:
            raise self.error
        return self.text


def fake_videos(*ids, handle="nasa"):
    """下载器 recognize() 返回的形状：按作品 id 从新到旧。"""
    return [{"id": ident, "url": f"https://www.tiktok.com/@{handle}/video/{ident}",
             "title": f"{handle} video {ident[-1]}", "duration": 40, "type": "video"}
            for ident in ids]


class FakeDownloader:
    """像真下载器一样：落盘 + 自己回调 content_register_download。

    「自己入库」这一点必须照做 —— 单条闭环刻意不自己建 ContentItem，
    靠的就是下载器这条回调。假对象不回调的话，测的就不是真实链路了。
    """

    def __init__(self, bridge, subtitle=True, fail_ids=(), notify=True):
        self.bridge = bridge
        self.subtitle = subtitle
        self.fail_ids = set(fail_ids)
        self.notify = notify                  # False = 下载成功但不回调入库（模拟交接断链）
        self.calls = []                       # 每次 download 收到的 video id 列表

    def download(self, videos, folder, quality, retry_count=3, concurrency=1):
        self.calls.append([video["id"] for video in videos])
        ok, failed = 0, []
        for video in videos:
            if video["id"] in self.fail_ids:
                failed.append({"id": video["id"], "error": "网络请求超时（假）"})
                continue
            media = str(Path(folder) / f"{video['id']}.mp4")
            Path(media).write_bytes(b"fake video bytes")
            subtitles = []
            if self.subtitle:
                subtitle_path = str(Path(folder) / f"{video['id']}.en.srt")
                Path(subtitle_path).write_text(SUBTITLE_TEXT, encoding="utf-8")
                subtitles.append(subtitle_path)
            if self.notify:
                self.bridge.content_register_download({
                    "id": video["id"], "title": video["title"], "url": video["url"],
                    "description": "", "cover": "", "duration": video.get("duration") or 0,
                    "folder": str(folder), "media": media, "subtitles": subtitles,
                    "state": "done"})
            ok += 1
        return {"ok": ok, "failed": failed, "folder": str(folder)}

    def downloaded_ids(self):
        return [ident for call in self.calls for ident in call]


class RunOneCreatorCase(unittest.TestCase):
    """公共脚手架：真 store / 真设置 / 真采集器 + 假下载器 / 假模型 / 假 ASR。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.folder = self.root / "downloads"
        self.folder.mkdir()
        self.http = CountingHttp()
        self.transcriber = FakeTranscriber()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def build(self, videos=None, by_handle=None, subtitle=True, fail_ids=(),
              api_key="sk-test", asr=False, video_path=None, notify=True):
        from content_bridge import ContentFactoryApi

        store = FactoryStore(self.root / "content-factory.db")
        settings = FactorySettings(self.root)
        settings.update("storage",
                        {"video_path": str(self.folder) if video_path is None else video_path})
        settings.update("collect", {"interval_minutes": 30})
        if api_key:
            settings.update("ai", {"api_key": api_key, "model": "deepseek-chat",
                                   "api_base": "https://api.deepseek.com/v1"})
        self.bridge = ContentFactoryApi(downloader=None, store=store, settings=settings)
        self.downloader = FakeDownloader(self.bridge, subtitle=subtitle, fail_ids=fail_ids,
                                         notify=notify)
        self.bridge.bridge_downloader = self.downloader
        self.api = CollectorApi(
            store, settings=settings, downloader=self.downloader,
            source=StaticCandidateSource(
                videos=fake_videos("7312000000000000003", "7312000000000000002",
                                   "7312000000000000001") if videos is None else videos,
                complete=True, by_handle=by_handle or {}))
        self.api.pipeline = ContentPipeline(
            store, settings, emit=self.api._emit, downloader=self.downloader,
            transcribe=self.transcriber if asr else None)
        self.api.pipeline._enricher = EnrichmentService(settings, http=self.http)
        return self.api

    def add_creator(self, handle="nasa", **fields):
        result = self.api.content_creator_save({"handle": handle, **fields})
        self.assertTrue(result["ok"], result)
        return result["creatorId"]

    def run_one(self, creator_id, **kwargs):
        return self.api.content_run_one_creator(creator_id, background=False, **kwargs)

    def items(self):
        return {row["source_video_id"]: row for row in self.bridge.content_items()["items"]}

    def jobs(self):
        return {row["source_video_id"]: row for row in self.api.collector.jobs.jobs(limit=50)}


class OneItemBoundaryTests(RunOneCreatorCase):
    """最重要的边界：一个 Creator、一条新内容。"""

    def test_case_a_only_the_newest_of_three_is_downloaded(self):
        self.build()
        creator_id = self.add_creator()
        result = self.run_one(creator_id)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["stage"], "done")
        self.assertEqual(self.downloader.downloaded_ids(), ["7312000000000000003"],
                         "3 条新内容里只能下载最新那 1 条")
        self.assertEqual(len(self.downloader.calls), 1, "下载器只能被调用一次")
        self.assertEqual(result["sourceVideoId"], "7312000000000000003")

        # 另外两条只是排队等待，不能被下载、也不能进内容库
        items = self.items()
        self.assertEqual(list(items), ["7312000000000000003"])
        jobs = self.jobs()
        self.assertEqual(jobs["7312000000000000003"]["state"], "done")
        self.assertEqual(jobs["7312000000000000002"]["state"], "pending")
        self.assertEqual(jobs["7312000000000000001"]["state"], "pending")

    def test_case_i_clicking_twice_does_not_download_a_second_video(self):
        self.build()
        creator_id = self.add_creator()
        self.run_one(creator_id)
        again = self.run_one(creator_id)

        self.assertTrue(again["ok"], again)
        self.assertEqual(again["processed"], 0)
        self.assertEqual(again["stage"], "no_content")
        self.assertEqual(self.downloader.downloaded_ids(), ["7312000000000000003"])
        self.assertEqual(len(self.items()), 1, "连点两次不能多出第二条内容")

    def test_case_j_never_takes_another_creators_job(self):
        """两个 Creator 都有待办时，点 A 只能处理 A 的内容，绝不碰 B 的。"""
        self.build(by_handle={
            "nasa": fake_videos("7312000000000000005", "7312000000000000004",
                                "7312000000000000003", handle="nasa"),
            "spacex": fake_videos("7312000000000000009", "7312000000000000008", handle="spacex")})
        nasa = self.add_creator("nasa")
        spacex = self.add_creator("spacex")
        # B 先被采集排上队；A 只排了最旧那条（另外两条这轮才是新的）
        self.api.collector.poll_creator(spacex)
        self.api.collector.source.by_handle["nasa"] = fake_videos(
            "7312000000000000003", handle="nasa")
        self.api.collector.poll_creator(nasa)
        self.api.collector.source.by_handle["nasa"] = fake_videos(
            "7312000000000000005", "7312000000000000004", "7312000000000000003", handle="nasa")
        spacex_before = {key: row["state"] for key, row in self.jobs().items()
                         if row["creator_handle"] == "spacex"}
        self.assertEqual(len(spacex_before), 2)

        result = self.run_one(nasa)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["processed"], 1)
        self.assertEqual(self.downloader.downloaded_ids(), ["7312000000000000005"],
                         "只能下载 A 自己最新那条新内容")
        for video_id in ("7312000000000000009", "7312000000000000008"):
            self.assertNotIn(video_id, self.downloader.downloaded_ids(),
                             "不能下载别的 Creator 的视频")
        for key, state in spacex_before.items():
            self.assertEqual(self.jobs()[key]["state"], state,
                             "别的 Creator 的任务状态不能被动过")

    def test_case_j_with_nothing_new_reports_zero_and_still_touches_nothing(self):
        self.build(by_handle={
            "nasa": fake_videos("7312000000000000003", handle="nasa"),
            "spacex": fake_videos("7312000000000000009", handle="spacex")})
        nasa = self.add_creator("nasa")
        spacex = self.add_creator("spacex")
        self.api.collector.poll_creator(nasa)
        self.api.collector.poll_creator(spacex)

        result = self.run_one(nasa)                    # nasa 的新内容已在队列里

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["processed"], 0)
        self.assertEqual(self.downloader.downloaded_ids(), [])
        self.assertEqual(self.jobs()["7312000000000000009"]["state"], "pending")


class DuplicateContentTests(RunOneCreatorCase):
    """重复内容：已经在库里或队列里的，不能重复下载。"""

    def test_case_b_the_latest_one_already_done_takes_the_next(self):
        self.build()
        creator_id = self.add_creator()
        first = self.run_one(creator_id)
        self.assertEqual(first["sourceVideoId"], "7312000000000000003")

        # 把第二条也放进队列（模拟上一轮检查已经排好、但还没下载）
        pending = [row for row in self.api.collector.jobs.pending(limit=10, creator_id=creator_id)]
        self.assertTrue(pending)
        self.api.collector.jobs.mark_failed(pending[0]["id"], "上一轮留下的失败任务")   # 移出队列
        second = self.run_one(creator_id)

        self.assertTrue(second["ok"], second)
        self.assertEqual(second["processed"], 1)
        self.assertEqual(second["sourceVideoId"], "7312000000000000002",
                         "最新那条已处理时应该取更新的下一条")
        self.assertEqual(self.downloader.downloaded_ids(),
                         ["7312000000000000003", "7312000000000000002"])

    def test_case_c_everything_already_present_is_not_an_error(self):
        self.build()
        creator_id = self.add_creator()
        # 库里已经有三条（手工登记，模拟以前下载过）
        for ident in ("7312000000000000003", "7312000000000000002", "7312000000000000001"):
            self.bridge.content_register_download({
                "id": ident, "title": f"old {ident[-1]}",
                "url": f"https://www.tiktok.com/@nasa/video/{ident}",
                "folder": str(self.folder), "media": "", "subtitles": [], "state": "done"})

        result = self.run_one(creator_id)

        self.assertTrue(result["ok"], "「没有新内容」不是异常")
        self.assertEqual(result["processed"], 0)
        self.assertEqual(result["stage"], "no_content")
        self.assertIn("没有发现新的可处理内容", result["message"])
        self.assertEqual(self.downloader.calls, [], "一条都不该下载")
        self.assertEqual(self.jobs(), {}, "没有新内容就不该建任务")


class PipelineStageTests(RunOneCreatorCase):
    """下载之后的每一段：字幕 / ASR / AI，以及失败时要保住内容。"""

    def test_case_d_subtitle_present_goes_all_the_way_to_ai_done(self):
        self.build()
        creator_id = self.add_creator()
        result = self.run_one(creator_id)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["transcriptState"], "done")
        self.assertEqual(result["aiState"], "done")
        self.assertFalse(self.transcriber.calls, "有字幕时不该动用 ASR")
        self.assertEqual(len(self.http.calls), 1, "AI 应该只被调用一次")
        item = self.items()["7312000000000000003"]
        self.assertEqual(item["download_status"], "done")
        self.assertTrue(item["transcript_text"].strip())
        self.assertTrue(item["local_video_path"].endswith(".mp4"))
        self.assertTrue(item["local_subtitle_path"].endswith(".en.srt"))
        self.assertIsNotNone(item["enrichment"], "AI 标注结果要真的落库")

    def test_case_e_no_subtitle_uses_local_asr_then_ai(self):
        self.build(subtitle=False, asr=True)
        creator_id = self.add_creator()
        with patch("content_factory.pipeline.asr_available", return_value=True):
            result = self.run_one(creator_id)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["transcriptState"], "done")
        self.assertEqual(result["aiState"], "done")
        self.assertEqual(len(self.transcriber.calls), 1, "没有字幕时才该走本机 ASR")
        self.assertTrue(self.transcriber.calls[0].endswith(".mp4"))
        item = self.items()["7312000000000000003"]
        self.assertEqual(item["transcript_text"], ASR_TEXT)

    def test_case_f_no_subtitle_and_no_asr_keeps_the_content_but_fails(self):
        self.build(subtitle=False, asr=False)
        creator_id = self.add_creator()
        result = self.run_one(creator_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "transcript")
        self.assertTrue(result["needsAsr"])
        self.assertIn("语音识别", result["error"] + result["message"],
                      "失败原因要指向「语音识别（ASR）」这一层，而不是一句「失败」")
        item = self.items()["7312000000000000003"]
        self.assertEqual(item["download_status"], "done", "下载好的内容不能因为转写失败就消失")
        self.assertEqual(item["transcript_status"], "failed")
        self.assertNotEqual(item["ai_status"], "done", "转写都没成，AI 不能显示完成")
        self.assertEqual(self.http.calls, [], "转写失败就不该去调模型")

    def test_case_g_missing_api_key_keeps_download_and_says_needs_api_key(self):
        self.build(api_key="")
        creator_id = self.add_creator()
        result = self.run_one(creator_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "enrich")
        self.assertTrue(result["needsApiKey"])
        self.assertIn("API Key", result["error"])
        item = self.items()["7312000000000000003"]
        self.assertEqual(item["download_status"], "done", "没配 Key 不能让下载好的视频回滚")
        self.assertEqual(item["ai_status"], "failed")
        self.assertEqual(self.http.calls, [])

    def test_case_h_download_failure_stops_before_transcript_and_ai(self):
        self.build(fail_ids={"7312000000000000003"})
        creator_id = self.add_creator()
        result = self.run_one(creator_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "download")
        self.assertIn("下载失败", result["error"])
        self.assertEqual(self.jobs()["7312000000000000003"]["state"], "failed")
        self.assertEqual(self.items(), {}, "没下下来就不该有内容记录")
        self.assertEqual(self.transcriber.calls, [], "下载失败不该往下走转写")
        self.assertEqual(self.http.calls, [], "下载失败不该往下走 AI")

    def test_missing_library_record_is_reported_not_silently_successful(self):
        """下载器说成功、内容库却没有记录：必须明确报出来，不能当成功。"""
        self.build(notify=False)                # 下载成功，但交接断链（不入库）
        creator_id = self.add_creator()

        result = self.run_one(creator_id)

        self.assertFalse(result["ok"], "内容库没有记录就不能算成功")
        self.assertEqual(result["stage"], "library")
        self.assertIn("内容库", result["error"])
        self.assertIn("没有这条记录", result["error"])
        self.assertEqual(self.items(), {})
        self.assertEqual(self.jobs()["7312000000000000003"]["state"], "done",
                         "下载器报告成功，任务本身仍然按完成收尾")


class PreflightTests(RunOneCreatorCase):
    """前置检查：能在起后台线程之前查出来的问题，必须当场返回。"""

    def test_missing_download_folder_is_refused_immediately(self):
        self.build(video_path="")
        creator_id = self.add_creator()
        result = self.run_one(creator_id)

        self.assertFalse(result["ok"])
        self.assertFalse(result.get("started", False))
        self.assertTrue(result["needsFolder"])
        self.assertIn("下载目录", result["error"])
        self.assertIn("存储设置", result["error"])
        self.assertEqual(self.downloader.calls, [])

    def test_missing_creator_is_reported(self):
        self.build()
        result = self.run_one("creator_does_not_exist")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "preflight")
        self.assertIn("不存在", result["error"])

    def test_disabled_creator_is_not_run(self):
        self.build()
        creator_id = self.add_creator()
        self.api.content_creator_toggle(creator_id, False)
        result = self.run_one(creator_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "preflight")
        self.assertIn("暂停", result["error"])
        self.assertEqual(self.downloader.calls, [])
        self.assertEqual(self.jobs(), {}, "停用的 Creator 不该因为点了一下就被排任务")

    def test_tiktok_verification_is_named_as_the_reason(self):
        self.build()
        creator_id = self.add_creator()
        self.api.collector.source = StaticCandidateSource(
            error="需要安全验证", needs_verification=True)
        result = self.run_one(creator_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "check")
        self.assertTrue(result["needsVerification"])
        self.assertIn("安全验证", result["error"])


class BackgroundRunTests(RunOneCreatorCase):
    """后台执行 + 可查询的真实状态：界面不能点了以后什么都不知道。"""

    def test_run_returns_immediately_and_status_reports_every_step(self):
        self.build()
        creator_id = self.add_creator()
        started = self.api.content_run_one_creator(creator_id)

        self.assertTrue(started["started"])
        self.assertTrue(started["runId"])
        self.assertEqual(started["handle"], "nasa")
        self.assertNotIn("result", started, "后台模式立刻返回，不带结果")

        run = None
        for _ in range(200):
            run = self.api.content_run_one_status(started["runId"])["run"]
            if run and run["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertIsNotNone(run)
        self.assertEqual(run["status"], "done", run)
        self.assertEqual(run["stage"], "done")
        self.assertEqual(run["result"]["processed"], 1)
        self.assertEqual(run["result"]["itemId"], self.items()["7312000000000000003"]["id"])
        stages = [entry["stage"] for entry in run["steps"]]
        for expected in ("checking", "discovered", "downloading", "downloaded",
                         "transcribing", "done"):
            self.assertIn(expected, stages, f"状态里缺少 {expected} 这一步")

    def test_status_can_be_queried_by_creator(self):
        self.build()
        creator_id = self.add_creator()
        started = self.api.content_run_one_creator(creator_id)
        for _ in range(200):
            run = self.api.content_run_one_status(creator_id=creator_id)["run"]
            if run and run["status"] in ("done", "failed"):
                break
            time.sleep(0.05)
        self.assertEqual(run["runId"], started["runId"])
        self.assertEqual(run["creatorId"], creator_id)

    def test_unknown_run_returns_none_instead_of_failing(self):
        self.build()
        self.assertIsNone(self.api.content_run_one_status("run_nope")["run"])


if __name__ == "__main__":
    unittest.main()
