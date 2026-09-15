"""AI 打标签：纯逻辑 + Api 层。

分两半：
- 纯逻辑（解析、规范化、合并、端点拼接、提供方预设）—— 不起 Api，完全离线
- Api.tag_assets —— 用假的模型调用打桩，验证"该打的打了、该跳的跳了、
  失败不写坏数据、能取消"

绝大多数用例是**回归网**：模型回复的格式千奇百怪，标签写回时又必须保住
meta 里原有的字段（写坏一次就是一条素材的元数据没了）。
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
from ai_tagging import (  # noqa: E402
    DEFAULT_PROVIDER,
    MAX_TAGS,
    MAX_TAG_LENGTH,
    TRANSCRIPT_LIMIT,
    build_tag_prompt,
    merge_tags,
    meta_summary,
    meta_tags,
    normalize_tags,
    parse_tag_response,
    provider_defaults,
    resolve_endpoint,
    tag_input_text,
)
from asset_index import library_rows, scan_assets  # noqa: E402
from web_app import Api  # noqa: E402


class ParseResponseTests(unittest.TestCase):
    """模型回复的格式千奇百怪，解析必须稳。"""

    def test_a_plain_json_object(self):
        self.assertEqual(parse_tag_response('{"tags": ["太空", "航天"], "summary": "月球计划"}'),
                         (["太空", "航天"], "月球计划"))

    def test_a_fenced_code_block(self):
        text = '```json\n{"tags": ["太空"], "summary": "x"}\n```'
        self.assertEqual(parse_tag_response(text), (["太空"], "x"))

    def test_prose_around_the_json(self):
        text = '好的，这是标签：\n{"tags": ["美食"], "summary": "y"}\n希望有用！'
        self.assertEqual(parse_tag_response(text), (["美食"], "y"))

    def test_a_refusal_yields_nothing(self):
        # 不能把"抱歉我做不到"当成标签存进去
        self.assertEqual(parse_tag_response("抱歉，我无法完成。"), ([], ""))

    def test_an_array_is_not_an_object(self):
        self.assertEqual(parse_tag_response('["太空"]'), ([], ""))

    def test_tags_of_the_wrong_type(self):
        self.assertEqual(parse_tag_response('{"tags": "太空"}')[0], [])
        # 只有 summary 时标签为空、概括照常返回 —— 调用方靠"标签为空"拒绝写入
        tags, summary = parse_tag_response('{"summary": "只有概括"}')
        self.assertEqual(tags, [])
        self.assertEqual(summary, "只有概括")

    def test_empty_input(self):
        self.assertEqual(parse_tag_response(""), ([], ""))
        self.assertEqual(parse_tag_response(None), ([], ""))

    def test_the_summary_is_capped(self):
        tags, summary = parse_tag_response(json.dumps({"tags": ["a"], "summary": "长" * 400}))
        self.assertLessEqual(len(summary), 120)


class NormalizeTagsTests(unittest.TestCase):
    def test_it_strips_the_hash_prefix(self):
        self.assertEqual(normalize_tags(["#太空", "太空"]), ["太空"])

    def test_dedupe_is_case_insensitive(self):
        self.assertEqual(normalize_tags(["NASA", "nasa", "Nasa"]), ["NASA"])

    def test_it_drops_blanks_and_caps_the_length(self):
        self.assertEqual(normalize_tags(["  ", "", "x" * 40]), ["x" * MAX_TAG_LENGTH])

    def test_it_caps_the_count(self):
        self.assertEqual(len(normalize_tags([f"标签{i}" for i in range(50)])), MAX_TAGS)

    def test_non_list_input(self):
        self.assertEqual(normalize_tags(None), [])
        self.assertEqual(normalize_tags("太空"), [])


class MergeTagsTests(unittest.TestCase):
    """写回 meta 时**绝不能**碰其它字段。"""

    META = {"schema": 1, "id": "111", "title": "标题", "description": "文案",
            "stats": {"views": 5}, "files": {"media": "a.mp4"},
            "author": {"username": "nasa"}}

    def test_every_original_field_survives(self):
        merged = merge_tags(self.META, ["太空"], "概括", "deepseek", "deepseek-chat", "T")
        for key, value in self.META.items():
            self.assertEqual(merged[key], value, key)
        self.assertEqual(merged["ai"]["tags"], ["太空"])
        self.assertEqual(merged["ai"]["provider"], "deepseek")
        self.assertEqual(merged["ai"]["tagged_at"], "T")

    def test_it_does_not_mutate_the_input(self):
        merge_tags(self.META, ["太空"], "概括")
        self.assertNotIn("ai", self.META)

    def test_a_second_pass_keeps_the_previous_provider_when_blank(self):
        first = merge_tags({}, ["a"], "s", "deepseek", "deepseek-chat", "T1")
        second = merge_tags(first, ["b"], "", "", "", "")
        self.assertEqual(second["ai"]["provider"], "deepseek")
        self.assertEqual(second["ai"]["model"], "deepseek-chat")
        self.assertEqual(second["ai"]["summary"], "s", "没给新概括时不该把旧的清空")

    def test_reading_tags_back(self):
        merged = merge_tags({}, ["太空", "航天"], "s")
        self.assertEqual(meta_tags(merged), ["太空", "航天"])
        self.assertEqual(meta_summary(merged), "s")

    def test_reading_tolerates_missing_or_broken_sections(self):
        self.assertEqual(meta_tags({}), [])
        self.assertEqual(meta_tags(None), [])
        self.assertEqual(meta_tags({"ai": "不是字典"}), [])
        self.assertEqual(meta_tags({"ai": {"tags": "不是列表"}}), [])
        self.assertEqual(meta_summary({"ai": None}), "")


class EndpointTests(unittest.TestCase):
    def test_the_three_ways_people_fill_the_base(self):
        self.assertEqual(resolve_endpoint("https://api.deepseek.com"),
                         "https://api.deepseek.com/chat/completions")
        self.assertEqual(resolve_endpoint("https://api.deepseek.com/v1"),
                         "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(resolve_endpoint("https://api.deepseek.com/v1/chat/completions"),
                         "https://api.deepseek.com/v1/chat/completions")

    def test_blank_stays_blank(self):
        # 空 base 要能被认出来，调用方据此报"没配地址"，而不是拼出个 /chat/completions
        self.assertEqual(resolve_endpoint(""), "")
        self.assertEqual(resolve_endpoint("   "), "")
        self.assertEqual(resolve_endpoint(None), "")

    def test_trailing_slash(self):
        self.assertEqual(resolve_endpoint("https://x/v1/"), "https://x/v1/chat/completions")


class ProviderTests(unittest.TestCase):
    def test_the_known_providers(self):
        self.assertEqual(provider_defaults("openai")["api_base"], "https://api.openai.com/v1")
        self.assertEqual(provider_defaults("deepseek")["api_base"], "https://api.deepseek.com/v1")
        self.assertEqual(provider_defaults("deepseek")["model"], "deepseek-chat")

    def test_an_unknown_provider_falls_back_instead_of_raising(self):
        self.assertEqual(provider_defaults("不存在的"), provider_defaults(DEFAULT_PROVIDER))
        self.assertEqual(provider_defaults(None), provider_defaults(DEFAULT_PROVIDER))

    def test_custom_has_no_preset(self):
        self.assertEqual(provider_defaults("custom")["api_base"], "")


class PromptTests(unittest.TestCase):
    def test_it_carries_all_three_inputs(self):
        prompt = build_tag_prompt("月球计划", "NASA 宣布", "We are going back")
        for needle in ("月球计划", "NASA 宣布", "We are going back"):
            self.assertIn(needle, prompt["user"])
        self.assertTrue(prompt["system"])

    def test_missing_parts_are_marked_not_omitted(self):
        prompt = build_tag_prompt("只有标题")
        self.assertIn("（无）", prompt["user"])
        self.assertIn("（无字幕）", prompt["user"])

    def test_a_huge_transcript_is_truncated(self):
        prompt = build_tag_prompt("t", "d", "字" * 50000)
        self.assertLess(len(prompt["user"]), TRANSCRIPT_LIMIT + 1000)

    def test_tag_input_text_detects_nothing_to_work_with(self):
        self.assertFalse(tag_input_text("", "", ""))
        self.assertFalse(tag_input_text(None, None, None))
        self.assertTrue(tag_input_text("标题"))
        self.assertTrue(tag_input_text("", "", "字幕"))


class TagApiTests(unittest.TestCase):
    """Api.tag_assets：认路径、跳过没 meta 的、写回不丢字段。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.api._learning.update({"api_key": "test-key", "provider": "deepseek",
                                   "model": "deepseek-chat"})
        self.events = []
        self.api._emit = lambda function, value: self.events.append((function, value))
        self.folder = Path(self.temp.name) / "@nasa"
        self.folder.mkdir(parents=True, exist_ok=True)

    def make_asset(self, stem, meta=None, subtitle=None):
        media = self.folder / f"{stem}.mp4"
        media.write_bytes(b"video")
        if meta is not None:
            (self.folder / f"{stem}.meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        if subtitle:
            (self.folder / f"{stem}.en.vtt").write_text(subtitle, encoding="utf-8")
        return media

    def run_tag(self, reply='{"tags": ["太空"], "summary": "概括"}', **kwargs):
        calls = []

        def fake_call(messages, max_tokens=6000, temperature=.25):
            calls.append(messages)
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch.object(self.api, "_call_model", side_effect=fake_call):
            result = self.api.tag_assets(str(self.folder), **kwargs)
        return result, calls

    def read_meta(self, stem):
        return json.loads((self.folder / f"{stem}.meta.json").read_text(encoding="utf-8"))

    def test_it_writes_tags_into_the_existing_meta(self):
        self.make_asset("Clip", {"schema": 1, "id": "111", "title": "月球计划",
                                 "stats": {"views": 5}, "files": {"media": "Clip.mp4"}})
        result, calls = self.run_tag()
        self.assertTrue(result["ok"])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(calls), 1)
        meta = self.read_meta("Clip")
        self.assertEqual(meta["ai"]["tags"], ["太空"])
        self.assertEqual(meta["ai"]["provider"], "deepseek")
        self.assertEqual(meta["id"], "111", "原有字段必须还在")
        self.assertEqual(meta["stats"], {"views": 5})
        self.assertEqual(meta["files"], {"media": "Clip.mp4"})

    def test_it_skips_assets_without_meta(self):
        # 没有 meta 就没标题没文案，只有文件名不值得花一次调用
        self.make_asset("NoMeta")
        result, calls = self.run_tag()
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"]["no_meta"], 1)
        self.assertEqual(calls, [], "不该为它调模型")

    def test_it_skips_assets_that_already_have_tags(self):
        self.make_asset("Tagged", {"title": "t", "ai": {"tags": ["旧标签"]}})
        result, calls = self.run_tag()
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"]["tagged"], 1)
        self.assertEqual(calls, [])

    def test_force_re_tags(self):
        self.make_asset("Tagged", {"title": "t", "ai": {"tags": ["旧标签"]}})
        result, _ = self.run_tag(force=True)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(self.read_meta("Tagged")["ai"]["tags"], ["太空"])

    def test_it_only_tags_the_paths_it_was_given(self):
        # 打标签认路径，所以"搜索结果里那几条"是真的只打那几条
        first = self.make_asset("One", {"title": "一"})
        self.make_asset("Two", {"title": "二"})
        result, calls = self.run_tag(paths=[str(first)])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("ai", self.read_meta("One"))
        self.assertNotIn("ai", self.read_meta("Two"))

    def test_it_uses_the_subtitle_when_the_caption_is_thin(self):
        self.make_asset("WithSub", {"title": "标题"},
                        subtitle="WEBVTT\n\n00:00.000 --> 00:02.000\nWe are going back to the Moon\n")
        _, calls = self.run_tag()
        self.assertIn("We are going back to the Moon", calls[0][1]["content"])

    def test_it_skips_assets_with_nothing_to_read(self):
        # 文件名**不算**"能读的东西"：拿一串作品 id 去问模型只会换来胡编的标签
        self.make_asset("7636162447711227158", {"title": "", "description": "", "id": "1"})
        result, calls = self.run_tag()
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"]["empty"], 1)
        self.assertEqual(calls, [], "只有文件名时不该调模型")

    def test_a_subtitle_alone_is_enough_to_tag(self):
        # 没标题没文案，但有字幕 —— 这个是真有内容可读的
        self.make_asset("Plain", {"title": "", "description": "", "id": "1"},
                        subtitle="WEBVTT\n\n00:00.000 --> 00:02.000\nCooking pasta at midnight\n")
        result, calls = self.run_tag()
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("Cooking pasta at midnight", calls[0][1]["content"])

    def test_the_filename_is_only_a_fallback_label(self):
        self.make_asset("Clip", {"title": "月球计划"})
        _, calls = self.run_tag()
        self.assertIn("月球计划", calls[0][1]["content"])
        self.assertNotIn("Clip", calls[0][1]["content"].split("标题：")[1].split("\n")[0])

    def test_a_model_failure_does_not_write_garbage(self):
        self.make_asset("Clip", {"title": "标题"})
        result, _ = self.run_tag(reply="抱歉，我做不到")
        self.assertEqual(result["updated"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertNotIn("ai", self.read_meta("Clip"),
                         "解析不出标签时绝不能写一个空的 ai 段进去")

    def test_one_failure_does_not_stop_the_rest(self):
        self.make_asset("Bad", {"title": "一"})
        self.make_asset("Good", {"title": "二"})
        replies = ["垃圾回复", '{"tags": ["美食"], "summary": "s"}']

        def fake_call(messages, max_tokens=6000, temperature=.25):
            return replies.pop(0)

        with patch.object(self.api, "_call_model", side_effect=fake_call):
            result = self.api.tag_assets(str(self.folder))
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(result["failed"]), 1)

    def test_it_refuses_without_an_api_key(self):
        self.api._learning["api_key"] = ""
        self.make_asset("Clip", {"title": "t"})
        result, calls = self.run_tag()
        self.assertFalse(result["ok"])
        self.assertIn("API Key", result["error"])
        self.assertEqual(calls, [])

    def test_limit_caps_the_work(self):
        for name in ("A", "B", "C"):
            self.make_asset(name, {"title": name})
        result, calls = self.run_tag(limit=1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(calls), 1)

    def test_progress_events_always_end(self):
        self.make_asset("Clip", {"title": "t"})
        self.run_tag()
        entries = [value for function, value in self.events if function == "assetTagging"]
        self.assertTrue(entries)
        self.assertTrue(any(entry.get("done") for entry in entries), "缺少收尾事件")

    def test_cancelling_stops_early(self):
        for name in ("A", "B", "C"):
            self.make_asset(name, {"title": name})
        api = self.api

        def fake_call(messages, max_tokens=6000, temperature=.25):
            api._asset_tag_cancel.set()
            return '{"tags": ["x"], "summary": "s"}'

        with patch.object(api, "_call_model", side_effect=fake_call):
            result = api.tag_assets(str(self.folder))
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["updated"], 1)

    def test_the_tags_show_up_in_the_library_index(self):
        self.make_asset("Clip", {"title": "月球计划", "upload_date": "20260905"})
        self.run_tag()
        assets, _ = scan_assets(str(self.folder))
        row = library_rows(assets)[0]
        self.assertEqual(row["tags"], ["太空"])
        self.assertEqual(row["summary"], "概括")

    def test_an_untagged_asset_reports_empty_tags(self):
        self.make_asset("Clip", {"title": "t"})
        assets, _ = scan_assets(str(self.folder))
        self.assertEqual(library_rows(assets)[0]["tags"], [])


if __name__ == "__main__":
    unittest.main()
