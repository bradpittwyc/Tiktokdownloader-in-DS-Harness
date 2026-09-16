"""本地存储 / 设置项 / AI 标注解析 / 转写 / 闭环编排 的回归网。

全部离线：模型调用注入假的 HTTP 对象，不碰网络。锁住的行为都是验收项：
- 同一条内容重复入库只留一条
- 标注结果落 sqlite，重开 store 仍在（= 应用重启后记录还在）
- 模型返回的脏 JSON 能救回来（围栏 / 前后缀 / 字段缺失 / 类型写错 / 0–100 分）
- 没配 API Key 时状态为 failed 且原因明确；配好之后能重跑成功
- 字幕清洗去掉序号、时间轴、HTML 标签、重复行
- 无字幕 + 无 ASR 后端时给出明确失败原因，而不是假装成功
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_factory.ai_enrichment import (  # noqa: E402
    EnrichmentError, EnrichmentService, build_prompt, normalize_enrichment,
    parse_enrichment,)
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.pipeline import ContentPipeline  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402
from content_factory.transcript import clean_transcript, transcript_from_subtitle_file  # noqa: E402

GOOD_REPLY = json.dumps({
    "topic": "AI 科技",
    "subtopic": "AI 与就业",
    "cefr_level": "b2",
    "accent": "美音",
    "speech_speed": "偏快",
    "learning_value": 0.92,
    "keywords": ["collapse", "shift"],
    "expressions": [
        {"text": "the cost of thinking is collapsing", "meaning_zh": "思考成本崩塌式下降"},
        "that is the wrong question",
    ],
    "grammar_points": ["whether 引导宾语从句"],
    "key_sentences": [
        {"text": "I see a tool that lets one person do the work of a small team.",
         "translation_zh": "我看到的是一个让一个人干完小团队工作的工具。"}
    ],
    "summary_zh": "AI 的关键不是取代工作，而是思考成本急剧下降。",
    "recommended_task": "复述",
}, ensure_ascii=False)


class FakeResponse:
    def __init__(self, text):
        self._text = text

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


class FakeHttp:
    """按脚本依次返回预设回复，并记录调用次数。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)] if self.replies else ""
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)


class TempEnvCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.store = FactoryStore(self.root / "content-factory.db")
        self.settings = FactorySettings(self.root)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()


class EnrichmentParseTests(unittest.TestCase):
    def test_plain_json_is_parsed(self):
        data, _raw = parse_enrichment(GOOD_REPLY)
        self.assertEqual(data["topic"], "AI 科技")
        self.assertEqual(data["cefr_level"], "B2", "小写等级要归一化成大写")
        self.assertEqual(data["recommended_task"], "复述")
        self.assertEqual(len(data["expressions"]), 2)
        self.assertEqual(data["expressions"][1]["text"], "that is the wrong question",
                         "字符串形式的表达要包成 {text: ...}")
        self.assertTrue(data["key_sentences"][0]["translation_zh"])

    def test_code_fence_is_stripped(self):
        text = "```json\n" + GOOD_REPLY + "\n```"
        data, _raw = parse_enrichment(text)
        self.assertEqual(data["topic"], "AI 科技")

    def test_explanatory_prose_around_json_is_tolerated(self):
        text = "好的，这是分析结果：\n" + GOOD_REPLY + "\n希望有帮助！"
        data, _raw = parse_enrichment(text)
        self.assertEqual(data["subtopic"], "AI 与就业")

    def test_missing_fields_get_safe_defaults(self):
        data, _raw = parse_enrichment('{"topic": "效率"}')
        self.assertEqual(data["topic"], "效率")
        self.assertEqual(data["cefr_level"], "B1")
        self.assertEqual(data["speech_speed"], "中等")
        self.assertEqual(data["learning_value"], 0.0)
        self.assertEqual(data["keywords"], [])
        self.assertEqual(data["recommended_task"], "跟读")

    def test_percentage_score_is_rescaled(self):
        data = normalize_enrichment({"learning_value": 88})
        self.assertAlmostEqual(data["learning_value"], 0.88, places=2)

    def test_broken_types_do_not_crash(self):
        data = normalize_enrichment({"keywords": "a, b\nc", "grammar_points": None,
                                     "expressions": [{"phrase": "x", "meaning": "y"}],
                                     "cefr_level": "C2 (高级)", "speech_speed": "语速偏快"})
        self.assertEqual(data["keywords"], ["a", "b", "c"])
        self.assertEqual(data["grammar_points"], [])
        self.assertEqual(data["expressions"][0]["text"], "x")
        self.assertEqual(data["cefr_level"], "C2")
        self.assertEqual(data["speech_speed"], "偏快")

    def test_garbage_raises_a_clear_error(self):
        with self.assertRaises(EnrichmentError):
            parse_enrichment("抱歉，我无法分析这段内容。")

    def test_prompt_substitutes_every_placeholder(self):
        prompt = build_prompt(None, {"title": "T", "creator_handle": "emily",
                                     "description": "D", "duration": 37}, "hello world")
        self.assertIn("T", prompt)
        self.assertIn("@emily", prompt)
        self.assertIn("hello world", prompt)
        self.assertNotIn("{transcript}", prompt)

    def test_prompt_without_transcript_says_so(self):
        prompt = build_prompt(None, {"title": "T"}, "")
        self.assertIn("没有可用字幕文本", prompt)


class EnrichmentServiceTests(TempEnvCase):
    def test_missing_api_key_is_explicit(self):
        service = EnrichmentService(self.settings, http=FakeHttp([GOOD_REPLY]))
        self.assertFalse(service.configured())
        with self.assertRaises(EnrichmentError) as ctx:
            service.enrich({"title": "x"}, "text")
        self.assertIn("API Key", str(ctx.exception))

    def test_retry_once_when_first_reply_is_not_json(self):
        http = FakeHttp(["抱歉，我先解释一下思路。", GOOD_REPLY])
        self.settings.update("ai", {"api_key": "sk-test", "model": "deepseek-chat"})
        service = EnrichmentService(self.settings, http=http)
        data, raw, _payload, attempts = service.enrich({"title": "x"}, "some transcript")
        self.assertEqual(attempts, 2)
        self.assertEqual(len(http.calls), 2)
        self.assertIn("只输出一个 JSON 对象", http.calls[1]["json"]["messages"][-1]["content"])
        self.assertEqual(data["topic"], "AI 科技")
        self.assertEqual(raw, GOOD_REPLY)

    def test_http_failure_is_wrapped(self):
        self.settings.update("ai", {"api_key": "sk-test"})
        service = EnrichmentService(self.settings, http=FakeHttp([RuntimeError("connection reset")]))
        with self.assertRaises(EnrichmentError):
            service.enrich({"title": "x"}, "text")

    def test_api_base_gets_the_chat_completions_suffix(self):
        self.settings.update("ai", {"api_key": "sk-test", "api_base": "https://api.deepseek.com/v1"})
        http = FakeHttp([GOOD_REPLY])
        EnrichmentService(self.settings, http=http).enrich({"title": "x"}, "t")
        self.assertEqual(http.calls[0]["url"], "https://api.deepseek.com/v1/chat/completions")

    def test_api_base_already_containing_the_path_is_not_doubled(self):
        self.settings.update("ai", {"api_key": "sk-test",
                                    "api_base": "https://x.test/v1/chat/completions/"})
        http = FakeHttp([GOOD_REPLY])
        EnrichmentService(self.settings, http=http).enrich({"title": "x"}, "t")
        self.assertEqual(http.calls[0]["url"], "https://x.test/v1/chat/completions")


class TranscriptTests(unittest.TestCase):
    def test_srt_noise_is_removed(self):
        raw = ("1\n00:00:01,000 --> 00:00:03,000\n<i>Hello</i> there\n\n"
               "2\n00:00:03,000 --> 00:00:05,000\nHello there\n\n"
               "3\n00:00:05,000 --> 00:00:07,000\n[Music] welcome back\n")
        text = clean_transcript(raw)
        self.assertNotIn("-->", text)
        self.assertNotIn("<i>", text)
        self.assertNotIn("Music", text)
        self.assertEqual(text.count("Hello there"), 1, "重复行要去掉")
        self.assertIn("welcome back", text)

    def test_vtt_header_is_removed(self):
        text = clean_transcript("WEBVTT\n\n00:00.000 --> 00:02.000\njust one line\n")
        self.assertEqual(text, "just one line")

    def test_reading_a_subtitle_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.en.srt"
            path.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello world\n", encoding="utf-8")
            self.assertEqual(transcript_from_subtitle_file(path), "hello world")

    def test_missing_subtitle_returns_empty(self):
        self.assertEqual(transcript_from_subtitle_file(""), "")
        self.assertEqual(transcript_from_subtitle_file("Z:/nope/none.srt"), "")


class StoreTests(TempEnvCase):
    def test_duplicate_source_video_keeps_one_row(self):
        first = self.store.upsert_item("7312", title="A", creator_handle="emily")
        second = self.store.upsert_item("7312", title="A updated")
        self.assertEqual(first, second)
        self.assertEqual(self.store.counts()["total"], 1)
        self.assertEqual(self.store.item(first)["title"], "A updated")

    def test_data_survives_reopening_the_store(self):
        item_id = self.store.upsert_item("7313", title="T", creator_handle="emily")
        self.store.save_enrichment(item_id, {"topic": "AI 科技", "keywords": ["a"],
                                             "learning_value": 0.5})
        reopened = FactoryStore(self.root / "content-factory.db")
        view = reopened.item_view(item_id)
        self.assertEqual(view["enrichment"]["topic"], "AI 科技")
        self.assertEqual(view["enrichment"]["keywords"], ["a"])
        self.assertEqual(view["ai_status"], "done")

    def test_saving_twice_overwrites_the_single_enrichment(self):
        item_id = self.store.upsert_item("7314", title="T")
        self.store.save_enrichment(item_id, {"topic": "旧"})
        self.store.save_enrichment(item_id, {"topic": "新"})
        self.assertEqual(self.store.enrichment(item_id)["topic"], "新")

    def test_status_filters_and_counts(self):
        a = self.store.upsert_item("a1", title="a")
        b = self.store.upsert_item("a2", title="b")
        self.store.save_enrichment(a, {"topic": "x"})
        self.store.set_ai_status(b, "failed", "网络超时")
        counts = self.store.counts()
        self.assertEqual(counts["enriched"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual([row["id"] for row in self.store.items(status="failed")], [b])

    def test_search_matches_title_and_creator(self):
        self.store.upsert_item("s1", title="productivity habits", creator_handle="emily")
        self.store.upsert_item("s2", title="完全无关", creator_handle="other")
        self.assertEqual(len(self.store.items(search="productivity")), 1)
        self.assertEqual(len(self.store.items(search="emily")), 1)

    def test_content_key_is_stable(self):
        from content_factory.factory_store import content_key
        self.assertEqual(content_key("tiktok", "123"), "tiktok:123")


class SettingsTests(TempEnvCase):
    def test_defaults_are_complete_and_persisted(self):
        data = self.settings.load()
        self.assertIn("ai", data)
        self.assertIn("storage", data)
        self.assertTrue(data["ai"]["prompt_template"])
        self.settings.update("ai", {"model": "deepseek-reasoner"})
        self.assertEqual(FactorySettings(self.root).section("ai")["model"], "deepseek-reasoner")

    def test_secrets_are_never_returned_to_the_ui(self):
        self.settings.update("ai", {"api_key": "sk-secret"})
        public = self.settings.public()
        self.assertEqual(public["ai"]["api_key"], "")
        self.assertTrue(public["ai"]["apiKeySet"])

    def test_blank_secret_keeps_the_previous_value(self):
        self.settings.update("ai", {"api_key": "sk-keep"})
        self.settings.update("ai", {"api_key": "", "model": "m2"})
        stored = self.settings.section("ai")
        self.assertEqual(stored["api_key"], "sk-keep")
        self.assertEqual(stored["model"], "m2")

    def test_unknown_file_content_falls_back_to_defaults(self):
        self.settings.path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(FactorySettings(self.root).section("general")["theme"], "system")


class PipelineTests(TempEnvCase):
    def setUp(self):
        super().setUp()
        self.events = []
        self.pipeline = ContentPipeline(self.store, self.settings,
                                        emit=lambda name, payload: self.events.append((name, payload)))

    def test_ingest_deduplicates_and_links_the_creator(self):
        videos = [{"id": "v1", "title": "A", "url": "https://www.tiktok.com/@emily/video/1"},
                  {"id": "v1", "title": "A", "url": "https://www.tiktok.com/@emily/video/1"},
                  {"id": "v2", "title": "B", "url": "https://www.tiktok.com/@emily/video/2"}]
        result = self.pipeline.ingest_videos(videos)
        self.assertEqual(result["created"], 2)
        self.assertEqual(self.store.counts()["total"], 2)
        row = self.store.find_item_by_source("tiktok", "v1")
        self.assertEqual(row["creator_handle"], "emily")
        self.assertTrue(row["creator_id"])

    def test_local_import_finds_video_and_its_subtitle(self):
        folder = self.root / "videos"
        folder.mkdir()
        (folder / "clip.mp4").write_bytes(b"x")
        (folder / "clip.en.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nhello from local\n", encoding="utf-8")
        result = self.pipeline.create_from_local(folder)
        self.assertEqual(result["imported"], 1)
        item = self.store.item(result["ids"][0])
        self.assertEqual(item["download_status"], "done")
        self.assertTrue(item["local_subtitle_path"].endswith("clip.en.srt"))

    def test_ai_without_key_marks_failed_with_a_reason(self):
        self.pipeline.ingest_videos([{"id": "v9", "title": "T",
                                      "url": "https://www.tiktok.com/@x/video/9"}])
        item_id = self.store.find_item_by_source("tiktok", "v9")["id"]
        self.pipeline.set_transcript(item_id, "hello world this is a transcript")
        result = self.pipeline.enrich_one(item_id)
        self.assertFalse(result["ok"])
        self.assertTrue(result["needsApiKey"])
        row = self.store.item(item_id)
        self.assertEqual(row["ai_status"], "failed")
        self.assertIn("API Key", row["last_error"])

    def test_full_closure_with_a_fake_model(self):
        """今天要验收的那条链路：入库 → transcript → 标注 → 落库 → 可读回。"""
        self.settings.update("ai", {"api_key": "sk-test", "model": "deepseek-chat"})
        self.pipeline._enricher = EnrichmentService(self.settings, http=FakeHttp([GOOD_REPLY]))
        folder = self.root / "videos2"
        folder.mkdir()
        (folder / "clip.mp4").write_bytes(b"x")
        (folder / "clip.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nAI is changing how we think about work.\n",
            encoding="utf-8")
        item_id = self.pipeline.create_from_local(folder)["ids"][0]

        result = self.pipeline.enrich_one(item_id)
        self.assertTrue(result["ok"], result)
        view = self.store.item_view(item_id)
        self.assertEqual(view["ai_status"], "done")
        self.assertEqual(view["transcript_status"], "done")
        self.assertEqual(view["enrichment"]["topic"], "AI 科技")
        self.assertEqual(view["enrichment"]["cefr_level"], "B2")
        self.assertEqual(view["enrichment"]["learning_value"], 0.92)
        self.assertEqual(view["enrichment"]["model"], "deepseek-chat")
        # 进度事件要给到界面：至少包含 running 与 done
        states = [payload.get("state") for name, payload in self.events if name == "enrichProgress"]
        self.assertIn("running", states)
        self.assertIn("done", states)

    def test_drain_continues_after_one_item_fails(self):
        """单条失败不能让整批停摆。"""
        self.settings.update("ai", {"api_key": "sk-test"})
        http = FakeHttp([GOOD_REPLY, RuntimeError("boom")])
        self.pipeline._enricher = EnrichmentService(self.settings, http=http)
        ids = []
        for index in range(2):
            folder = self.root / f"batch{index}"
            folder.mkdir()
            (folder / "clip.mp4").write_bytes(b"x")
            (folder / "clip.srt").write_text(
                f"1\n00:00:01,000 --> 00:00:02,000\nline {index}\n", encoding="utf-8")
            ids.append(self.pipeline.create_from_local(folder)["ids"][0])
        results = self.pipeline.enrich_many(ids, background=False)
        self.assertEqual(results["queued"], 2)
        states = sorted(result["ok"] for result in results["results"])
        self.assertEqual(states, [False, True], "一条成功一条失败，队列继续跑完")

    def test_retry_after_fixing_the_key_succeeds(self):
        self.pipeline.ingest_videos([{"id": "v10", "title": "T"}])
        item_id = self.store.find_item_by_source("tiktok", "v10")["id"]
        self.pipeline.set_transcript(item_id, "text for analysis")
        self.assertFalse(self.pipeline.enrich_one(item_id)["ok"])
        self.assertEqual(self.store.item(item_id)["ai_status"], "failed")
        self.settings.update("ai", {"api_key": "sk-test"})
        self.pipeline._enricher = EnrichmentService(self.settings, http=FakeHttp([GOOD_REPLY]))
        self.assertTrue(self.pipeline.enrich_one(item_id)["ok"])
        self.assertEqual(self.store.item(item_id)["ai_status"], "done")
        self.assertEqual(self.store.item(item_id)["last_error"], "")

    def test_no_transcript_and_no_asr_gives_a_clear_reason(self):
        self.settings.update("ai", {"api_key": "sk-test"})
        self.pipeline._enricher = EnrichmentService(self.settings, http=FakeHttp([GOOD_REPLY]))
        self.pipeline.ingest_videos([{"id": "v11", "title": "T"}])
        item_id = self.store.find_item_by_source("tiktok", "v11")["id"]
        with patch("content_factory.pipeline.asr_available", return_value=False):
            result = self.pipeline.enrich_one(item_id)
        self.assertFalse(result["ok"])
        self.assertIn("语音识别", result["error"])
        self.assertEqual(self.store.item(item_id)["transcript_status"], "failed")

    def test_running_an_unknown_item_does_not_raise(self):
        self.assertFalse(self.pipeline.enrich_one("item_missing")["ok"])


class DemoDataTests(TempEnvCase):
    def test_seed_is_idempotent_and_marked_as_demo(self):
        from content_factory.mock_data import clear_demo_items, seed_demo_items
        first = seed_demo_items(self.store)
        self.assertGreaterEqual(first["created"], 10)
        second = seed_demo_items(self.store)
        self.assertEqual(second["created"], 0, "重复生成不应产生重复行")
        counts = self.store.counts()
        self.assertGreaterEqual(counts["enriched"], 6)
        self.assertGreaterEqual(counts["failed"], 2)
        self.assertGreaterEqual(counts["pending"], 2)
        removed = clear_demo_items(self.store)
        self.assertGreaterEqual(removed["removed"], 12)
        # 演示数据清干净之后，内容库必须真的空了（否则「一键清除」是假的）
        self.assertEqual(self.store.counts()["total"], 0)
        self.assertEqual(self.store.items(limit=5000), [])
        self.assertEqual(self.store.counts()["total"], 0)


class ErrorFeedTests(TempEnvCase):
    def test_log_lines_are_classified(self):
        from content_factory import errors_feed
        path = errors_feed.log_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("2026-01-15 14:28:32 下载失败: 请求超时\n"
                        "2026-01-15 14:30:00 开始下载\n", encoding="utf-8")
        result = errors_feed.recent_errors(root=self.root)
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"]["fromLog"], 1)
        self.assertEqual(result["entries"][0]["kind"], "网络请求超时")

    def test_store_failures_are_included(self):
        from content_factory import errors_feed
        item_id = self.store.upsert_item("e1", title="失败的作品")
        self.store.set_ai_status(item_id, "failed", "API 请求超时（504）")
        result = errors_feed.recent_errors(store=self.store, root=self.root)
        kinds = [entry["kind"] for entry in result["entries"]]
        self.assertIn("网络请求超时", kinds)
        self.assertEqual(result["summary"]["fromStore"], 1)

    def test_missing_log_is_not_an_error(self):
        from content_factory import errors_feed
        result = errors_feed.recent_errors(root=self.root)
        self.assertFalse(result["logExists"])
        self.assertEqual(result["entries"], [])


if __name__ == "__main__":
    unittest.main()
