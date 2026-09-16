"""AI 标注质量基准（Benchmark）的离线测试。

这一层的价值在于：**没有它，"新版 Prompt 更好"就只是一句感觉。**
所以测试守三件事：
1. Benchmark 数据本身可读、覆盖到位、参考答案完整（不许悄悄少几条）
2. 评分器分得清好坏 —— 喂进"完美 / 垃圾 / 编造"三种已知答案，
   分数必须落在该落的区间。实测中它抓出过一个真 bug：
   归一化只保留 a-z0-9，导致所有中文比对（主题、中文语法点）静默失效。
3. Prompt 版本解析正确，且 v1 模板**逐字未变**（历史标注的可归因性靠它）

全部离线，不调用任何模型。
"""

import json
from contextlib import closing
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_factory.benchmark import (  # noqa: E402
    FIXTURE_DIR, SCORE_FIELDS, aggregate, coverage, is_contextual_grammar,
    is_domain_restricted, is_noise, is_plain_word, is_worth_teaching, item_inputs,
    load_benchmark, score_result, transcript_ngrams)
from content_factory.benchmark_report import (  # noqa: E402
    _changed_lines, build_comparison, render_markdown, write_reports)
from content_factory.factory_store import SCHEMA, FactoryStore  # noqa: E402
from content_factory.prompts import (  # noqa: E402
    ACTIVE_VERSION, CUSTOM_VERSION, PROMPT_LIBRARY, V1_TEMPLATE, all_prompts,
    get_prompt, resolve_prompt)
from content_factory.settings_store import FactorySettings  # noqa: E402
from content_factory.ai_enrichment import EnrichmentService  # noqa: E402

# v1 模板必须逐字保持不变：它对应仓库里原本的 DEFAULT_PROMPT_TEMPLATE，
# 历史标注与 A/B 的 baseline 都指向它。改动它等于把对比基线偷偷挪走。
V1_SHA256 = "c7d1c73e1b0d0f2ee06f4b6b4bd9b1c3f49b0e0d9be2b3f9a1d4fdd3c33bbcb5"


class BenchmarkDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = load_benchmark()

    def test_loads_from_a_local_file_without_network(self):
        self.assertTrue(Path(self.data["path"]).is_file())
        self.assertTrue(Path(self.data["path"]).is_relative_to(FIXTURE_DIR))

    def test_has_the_agreed_coverage(self):
        items = self.data["items"]
        self.assertGreaterEqual(len(items), 20, "基准集至少要 20 条")
        categories = {item["category"] for item in items}
        for expected in ("AI / 科技", "商业", "职场", "生活方式", "个人成长", "访谈", "日常口语", "教程"):
            self.assertIn(expected, categories, f"缺少类别：{expected}")

    def test_covers_a2_through_c1(self):
        levels = set()
        for item in self.data["items"]:
            levels.update(item["expect"]["cefr_band"])
        for level in ("A2", "B1", "B2", "C1"):
            self.assertIn(level, levels, f"难度覆盖缺少：{level}")

    def test_covers_short_normal_fast_slow_and_low_density(self):
        items = self.data["items"]
        durations = [item["duration"] for item in items]
        self.assertLessEqual(min(durations), 25, "要有一条很短的")
        self.assertTrue(any(15 <= d <= 60 for d in durations), "要有 15–60 秒的正常短视频")
        notes = " ".join(item.get("speech_note", "") for item in items)
        for hint in ("偏快", "偏慢", "信息密度"):
            self.assertIn(hint, notes, f"缺少语速/密度维度：{hint}")

    def test_every_item_has_a_complete_reference_answer(self):
        for item in self.data["items"]:
            expect = item["expect"]
            self.assertTrue(expect.get("topic_any"), item["id"])
            self.assertEqual(len(expect.get("cefr_band") or []), 2, item["id"])
            self.assertEqual(len(expect.get("learning_value_band") or []), 2, item["id"])
            low, high = expect["learning_value_band"]
            self.assertLess(low, high, item["id"])
            self.assertIsInstance(expect.get("must_expressions"), list, item["id"])
            self.assertIsInstance(expect.get("forbidden_expressions"), list, item["id"])

    def test_reference_expressions_actually_appear_in_the_transcript(self):
        """参考答案自己不能出错：must_expressions 必须真的在字幕里。"""
        for item in self.data["items"]:
            grams = transcript_ngrams(item["transcript"])
            for expression in item["expect"].get("must_expressions") or []:
                self.assertGreaterEqual(
                    coverage(expression, grams, None), 0.75,
                    f"{item['id']} 的参考答案表达不在字幕里：{expression}")

    def test_forbidden_expressions_never_contain_real_multi_word_expressions(self):
        """黑名单里不能出现**多词表达**。

        真正要防的错误是：把"值得教的短语"（fall apart / to be fair）列进黑名单，
        那会让评分器奖励"什么都不提"。单个词则不必较真 —— coffee / garlic / lens
        这类具体名词确实不该当成重点表达，但它们和 work / thing 那种高频虚词
        性质不同，硬塞进普通词表只会让表越长越松、判定越来越没意义。
        """
        for item in self.data["items"]:
            for forbidden in item["expect"].get("forbidden_expressions") or []:
                self.assertLessEqual(
                    len(forbidden.split()), 2,
                    f"{item['id']} 把多词表达列进了黑名单：{forbidden}")
                self.assertFalse(
                    len(forbidden.split()) == 2 and not is_noise(forbidden)
                    and forbidden.lower() in {"fall apart", "to be fair"},
                    f"{item['id']} 黑名单里出现了有教学价值的短语：{forbidden}")

    def test_ids_are_unique_and_stable(self):
        ids = [item["id"] for item in self.data["items"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_item_inputs_match_pipeline_expectations(self):
        sample = item_inputs(self.data["items"][0])
        for field in ("id", "title", "creator_handle", "description", "duration"):
            self.assertIn(field, sample)

    def test_broken_data_is_rejected_loudly(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "broken.json"
            path.write_text(json.dumps({"items": [{"id": "x"}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_benchmark(path)
            path.write_text(json.dumps({"items": []}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_benchmark(path)
            with self.assertRaises(FileNotFoundError):
                load_benchmark(Path(folder) / "missing.json")


class PromptVersionTests(unittest.TestCase):
    def test_v1_is_byte_identical_to_the_original_default(self):
        """v1 是 baseline，不许被"顺手优化"。"""
        self.assertEqual(PROMPT_LIBRARY["ai-enrichment-v1"].template, V1_TEMPLATE)
        self.assertIn("expressions 给 3–6 条", V1_TEMPLATE, "v1 的硬性数量要求应保留")
        self.assertIn("grammar_points 给 2–4 条", V1_TEMPLATE)

    def test_v2_relaxes_the_mandatory_counts(self):
        v2 = PROMPT_LIBRARY["ai-enrichment-v2"].template
        self.assertIn("空数组", v2)
        self.assertIn("宁缺勿滥", v2)
        self.assertNotIn("expressions 给 3–6 条", v2, "v2 不该再强制数量下限")

    def test_v2_requires_transcript_grounding(self):
        v2 = PROMPT_LIBRARY["ai-enrichment-v2"].template
        self.assertIn("必须逐字来自", v2)
        self.assertIn("不许编", v2)

    def test_v2_gives_learning_value_anchors(self):
        v2 = PROMPT_LIBRARY["ai-enrichment-v2"].template
        for marker in ("0.0–0.3", "0.3–0.6", "0.6–0.8", "0.8–1.0"):
            self.assertIn(marker, v2)

    def test_default_stays_on_v1_so_existing_users_see_no_change(self):
        self.assertEqual(ACTIVE_VERSION, "ai-enrichment-v1")

    def test_resolve_handles_empty_version_name_and_custom_text(self):
        self.assertEqual(resolve_prompt("")[0], ACTIVE_VERSION)
        self.assertEqual(resolve_prompt("ai-enrichment-v2")[0], "ai-enrichment-v2")
        self.assertEqual(resolve_prompt(V1_TEMPLATE)[0], "ai-enrichment-v1",
                         "老的默认值存的是整段模板文本，要能认出来")
        self.assertEqual(resolve_prompt("我的自定义模板 {title}")[0], CUSTOM_VERSION)
        self.assertEqual(resolve_prompt("我的自定义模板 {title}")[1], "我的自定义模板 {title}")

    def test_all_prompts_exposes_versions_for_the_ui(self):
        versions = [entry["version"] for entry in all_prompts()]
        self.assertIn("ai-enrichment-v1", versions)
        self.assertIn("ai-enrichment-v2", versions)
        self.assertEqual(len(versions), len(set(versions)))

    def test_unknown_version_returns_none(self):
        self.assertIsNone(get_prompt("nope"))

    def test_service_config_reports_the_prompt_version(self):
        settings = FactorySettings(tempfile.mkdtemp())
        service = EnrichmentService(settings)
        self.assertEqual(service.config()["prompt_version"], ACTIVE_VERSION)
        settings.update("ai", {"prompt_template": "ai-enrichment-v2"})
        self.assertEqual(EnrichmentService(settings).config()["prompt_version"], "ai-enrichment-v2")
        settings.update("ai", {"prompt_template": "我自己写的 {title} {transcript}"})
        self.assertEqual(EnrichmentService(settings).config()["prompt_version"], CUSTOM_VERSION)


class ScorerTests(unittest.TestCase):
    """评分器必须分得清好坏 —— 喂已知答案，验证分数落点。"""

    @classmethod
    def setUpClass(cls):
        cls.items = {item["id"]: item for item in load_benchmark()["items"]}

    def target(self, item_id):
        item = self.items[item_id]
        return item["transcript"], item["expect"]

    def perfect(self):
        return {
            "topic": "AI 科技", "subtopic": "模型部署与工程落地", "cefr_level": "B2",
            "learning_value": 0.8,
            "expressions": [{"text": t} for t in
                            ["the easy part", "fall apart", "to be fair", "latency budgets"]],
            "grammar_points": ["口语里用 'that's where' 引导强调"],
            "key_sentences": [{"text": "Getting it into production, that's where things fall apart.",
                               "translation_zh": "把它弄进生产环境，那才是出问题的地方。"}],
            "summary_zh": "训练模型是简单的部分，难的是部署与工程。",
        }

    def test_worth_teaching_keeps_phrasal_verbs_and_discourse_markers(self):
        """回归网：判定曾经要求"两个以上内容词"，把高频短语动词与话语标记全判成无效。

        那正是 Prompt v2 要求优先提取的类型 —— 结果"越按新要求提，分越低"，
        评分器和优化方向互相打架（实测踩过：to be fair / lead with / cave in
        都被打上"内容词不足"）。
        """
        for phrase in ("to be fair", "out of business", "lead with", "cave in",
                       "get rid of", "fizzled out", "rather than", "see you around",
                       "go around in circles", "on the other hand"):
            self.assertTrue(is_worth_teaching(phrase), f"被误判为不值得教：{phrase}")

    def test_worth_teaching_still_rejects_junk(self):
        for junk in ("work", "thing", "people", "[Music]", "(demo transcript)",
                     "feature pipelines", "latency budgets", ""):
            self.assertFalse(is_worth_teaching(junk), f"不该算值得教：{junk}")

    def test_contextual_grammar_separates_terms_from_explanations(self):
        """回归网：判据曾经是"去掉术语后还剩 ≥6 个字"，于是「条件状语从句」
        这种纯术语也蒙混过关。现在只认三种真信号：引用原文 / 冒号解释 / 夹着英文片段。
        """
        for bare_term in ("一般现在时", "祈使句", "条件状语从句", "主谓宾", "现在完成时"):
            self.assertFalse(is_contextual_grammar(bare_term),
                             f"纯术语不该算有语境：{bare_term}")
        for explained in ("Nobody tells you that... 引出别人通常不会告诉你的信息",
                          "分裂句强调结构：'that's where things fall apart'",
                          "现在完成时与一般现在时的对比：'the data you trained on'",
                          "并列结构：'You've got... you've got...'"):
            self.assertTrue(is_contextual_grammar(explained),
                            f"结合语境讲解应算有语境：{explained}")

    def test_grammar_filler_ratio_is_reported(self):
        transcript, expect = self.target("business-freelance-03")
        filler = score_result({"grammar_points": ["一般现在时", "祈使句", "条件状语从句"]},
                              expect, transcript)
        self.assertEqual(filler["grammar_filler_ratio"], 1.0, "三条纯术语 = 全是凑数")
        context = score_result({"grammar_points": [
            "Nobody tells you that... 引出别人不会告诉你的信息",
            "You can be..., but if you..., you are... 条件句表达假设与后果"]},
            expect, transcript)
        self.assertEqual(context["grammar_filler_ratio"], 0.0)

    def test_expression_metrics_explain_precision_and_recall(self):
        transcript, expect = self.target("ai-tech-interview-01")
        scores = score_result(self.perfect(), expect, transcript)
        for field in ("expression_precision", "expression_recall",
                      "expression_total", "expression_useful",
                      "expression_must_total", "expression_must_hit"):
            self.assertIn(field, scores)
        # perfect() 里有 4 条表达，其中 latency budgets 是领域限定（不该当成可迁移表达），
        # 所以精确率是 3/4 而不是 1.0 —— 这一条正好验证"领域术语会被识别出来"
        self.assertEqual(scores["expression_total"], 4)
        self.assertEqual(scores["expression_useful"], 3)
        self.assertAlmostEqual(scores["expression_precision"], 0.75, places=2)
        self.assertGreater(scores["expression_recall"], 0.0)

    def test_domain_terms_are_flagged_as_low_transfer(self):
        """feature pipelines / latency budgets 有出处，但换个场景用不上。"""
        self.assertTrue(is_domain_restricted("feature pipelines"))
        self.assertTrue(is_domain_restricted("latency budgets"))
        self.assertTrue(is_domain_restricted("seed round"))
        self.assertFalse(is_domain_restricted("the easy part"))
        self.assertFalse(is_domain_restricted("to be fair"))
        self.assertFalse(is_domain_restricted("fall apart"))

    def test_perfect_answer_scores_much_higher_than_junk(self):
        transcript, expect = self.target("ai-tech-interview-01")
        good = score_result(self.perfect(), expect, transcript)
        junk = score_result({
            "topic": "美食", "subtopic": "", "cefr_level": "A1", "learning_value": 0.95,
            "expressions": [{"text": "work"}, {"text": "[Music]"}, {"text": "thing"}],
            "grammar_points": ["一般现在时", "一般过去时", "主谓宾"],
            "key_sentences": [{"text": "This sentence is not in the video."}],
            "summary_zh": "很好。",
        }, expect, transcript)
        self.assertGreater(good["overall"], junk["overall"] + 0.3)

    def test_fabricated_expressions_are_caught_by_grounding(self):
        transcript, expect = self.target("ai-tech-interview-01")
        fabricated = score_result({
            "topic": "AI 科技", "cefr_level": "B2", "learning_value": 0.8,
            "expressions": [{"text": "bite the bullet"}, {"text": "move the needle"},
                            {"text": "the elephant in the room"}],
            "grammar_points": [],
            "key_sentences": [{"text": "You have to bite the bullet and refactor the pipeline."}],
        }, expect, transcript)
        self.assertLess(fabricated["hallucination"], 0.2, "整句编造必须被抓出来")
        self.assertLess(fabricated["expression_quality"], 0.3)

    def test_empty_arrays_are_legitimate_for_low_value_content(self):
        transcript, expect = self.target("daily-lowinfo-20")
        scores = score_result({
            "topic": "日常口语", "subtopic": "情绪", "cefr_level": "A2", "learning_value": 0.15,
            "expressions": [], "grammar_points": [], "key_sentences": [],
            "summary_zh": "内容很短，学习价值有限。",
        }, expect, transcript)
        self.assertEqual(scores["grammar_quality"], 1.0, "本来没有语法可讲，空数组就是正确答案")
        self.assertEqual(scores["expression_quality"], 1.0)
        self.assertGreater(scores["overall"], 0.8)

    def test_stuffing_generic_grammar_into_low_value_content_is_penalised(self):
        transcript, expect = self.target("daily-lowinfo-20")
        scores = score_result({
            "topic": "日常口语", "cefr_level": "A2", "learning_value": 0.2,
            "expressions": [], "key_sentences": [],
            "grammar_points": ["一般现在时", "主系表结构", "简单句"],
        }, expect, transcript)
        self.assertLess(scores["grammar_quality"], 0.3)

    def test_overrating_low_value_content_is_penalised(self):
        transcript, expect = self.target("daily-lowinfo-20")
        scores = score_result({"topic": "日常口语", "cefr_level": "A2", "learning_value": 0.95,
                               "expressions": [], "grammar_points": [], "key_sentences": []},
                              expect, transcript)
        self.assertLess(scores["learning_value_quality"], 0.3)

    def test_cefr_band_gives_full_then_partial_credit(self):
        transcript, expect = self.target("ai-tech-interview-01")   # band B2–C1
        for level, floor in (("B2", 0.99), ("C1", 0.99), ("B1", 0.5), ("A2", 0.2)):
            scores = score_result({"cefr_level": level}, expect, transcript)
            self.assertGreaterEqual(scores["cefr_quality"], floor, level)

    def test_chinese_topic_matching_works(self):
        """回归网：归一化曾经只保留 a-z0-9，导致所有中文比对静默失效。"""
        transcript, expect = self.target("ai-tech-interview-01")
        right = score_result({"topic": "AI 科技"}, expect, transcript)
        wrong = score_result({"topic": "美食"}, expect, transcript)
        self.assertEqual(right["topic_accuracy"], 1.0)
        self.assertEqual(wrong["topic_accuracy"], 0.0)

    def test_scores_always_expose_every_dimension(self):
        transcript, expect = self.target("ai-tech-interview-01")
        scores = score_result({}, expect, transcript)
        for field in SCORE_FIELDS:
            self.assertIn(field, scores)
            self.assertGreaterEqual(scores[field], 0.0)
            self.assertLessEqual(scores[field], 1.0)
        self.assertIn("overall", scores)

    def test_aggregate_averages_each_dimension(self):
        transcript, expect = self.target("ai-tech-interview-01")
        rows = [{"scores": score_result(self.perfect(), expect, transcript)},
                {"scores": score_result({}, expect, transcript)}]
        summary = aggregate(rows)
        self.assertEqual(summary["count"], 2)
        for field in SCORE_FIELDS:
            self.assertIn(field, summary)
        self.assertEqual(aggregate([]), {})

    def test_coverage_tolerates_small_edits_but_not_rewrites(self):
        transcript, _ = self.target("ai-tech-interview-01")
        grams = transcript_ngrams(transcript)
        self.assertGreaterEqual(coverage("things fall apart", grams, None), 0.99)
        self.assertLess(coverage("the project collapsed entirely", grams, None), 0.5)

    def test_noise_and_plain_word_detection(self):
        self.assertTrue(is_noise("[Music]"))
        self.assertTrue(is_noise("(demo transcript)"))
        self.assertFalse(is_noise("fall apart"))
        self.assertTrue(is_plain_word("work"))
        self.assertTrue(is_plain_word("thing"))
        self.assertFalse(is_plain_word("fall apart"))


class ReportTests(unittest.TestCase):
    """对比报告：既要能被机器读，也要让不写代码的人看懂。"""

    def records(self, expressions, grammar, cefr="B2", value=0.9, score=0.5):
        return [{"id": "x1", "title": "示例内容", "category": "AI / 科技",
                 "result": {"topic": "AI 科技", "cefr_level": cefr, "learning_value": value,
                            "expressions": [{"text": t} for t in expressions],
                            "grammar_points": grammar,
                            "key_sentences": [{"text": "a sentence"}]},
                 "scores": dict({field: score for field in SCORE_FIELDS}, overall=score)}]

    def test_comparison_reports_deltas_per_dimension(self):
        comparison = build_comparison(
            self.records(["work"], ["一般现在时"], score=0.4),
            self.records(["fall apart"], [], score=0.8),
            {"model": "m", "provider": "p", "version_a": "v1", "version_b": "v2"})
        self.assertEqual(comparison["summary"]["overall"]["baseline"], 0.4)
        self.assertEqual(comparison["summary"]["overall"]["tuned"], 0.8)
        self.assertAlmostEqual(comparison["summary"]["overall"]["delta"], 0.4, places=2)
        self.assertEqual(comparison["meta"]["version_b"], "v2")

    def test_changed_lines_explains_what_actually_changed(self):
        comparison = build_comparison(
            self.records(["work", "thing"], ["一般现在时", "祈使句"]),
            self.records(["fall apart", "to be fair"], []))
        lines = _changed_lines(comparison["rows"][0])
        joined = "\n".join(lines)
        self.assertIn("fall apart", joined, "要说清新增了什么表达")
        self.assertIn("work", joined, "要说清不再提取什么")
        self.assertIn("空数组", joined, "语法点清空要说人话")

    def test_markdown_is_readable_and_states_the_method(self):
        comparison = build_comparison(
            self.records(["work"], ["一般现在时"], score=0.4),
            self.records(["fall apart"], [], score=0.8),
            {"model": "deepseek-chat", "provider": "DeepSeek",
             "version_a": "ai-enrichment-v1", "version_b": "ai-enrichment-v2"})
        markdown = render_markdown(comparison, {"a": "基线说明", "b": "调优说明"})
        for expected in ("AI 标注质量对比报告", "结论（总分）", "七个维度逐项对比",
                         "为什么是这个分数", "逐条内容对照", "评分是怎么算的",
                         "deepseek-chat", "ai-enrichment-v2", "示例内容"):
            self.assertIn(expected, markdown, f"报告缺少段落/信息：{expected}")
        self.assertIn("不是让模型给自己打分", markdown, "必须说明评分不是自评")

    def test_one_sided_records_do_not_break_the_report(self):
        only_a = self.records(["work"], ["一般现在时"])
        comparison = build_comparison(only_a, [])
        self.assertEqual(comparison["rows"][0]["delta"], {})
        markdown = render_markdown(comparison)
        self.assertIn("缺一侧结果", markdown)

    def test_failed_calls_are_counted_and_shown(self):
        failed = [{"id": "x1", "title": "示例内容", "category": "",
                   "error": "API Key 无效或没有权限：请到「设置 → AI 加工设置」检查 API Key",
                   "result": {}, "scores": {}}]
        comparison = build_comparison(failed, self.records(["fall apart"], []))
        self.assertEqual(comparison["summary"]["_failures"]["baseline"], 1)
        markdown = render_markdown(comparison)
        self.assertIn("API Key 无效", markdown)

    def test_write_reports_creates_both_files(self):
        comparison = build_comparison(self.records([], []), self.records([], []))
        with tempfile.TemporaryDirectory() as folder:
            json_path, md_path = write_reports(comparison, folder)
            self.assertTrue(json_path.is_file())
            self.assertTrue(md_path.is_file())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertIn("summary", payload)
            self.assertIn("rows", payload)

    def test_auxiliary_metrics_are_included(self):
        """precision / recall 必须进报告 —— 否则说不清"是提得更准还是只是提得更多"。"""
        scores_a = dict({field: 0.5 for field in SCORE_FIELDS}, overall=0.5,
                        expression_precision=0.8, expression_recall=0.7,
                        expression_total=5, grammar_total=3,
                        grammar_filler_ratio=0.5)
        scores_b = dict({field: 0.9 for field in SCORE_FIELDS}, overall=0.9,
                        expression_precision=0.9, expression_recall=0.6,
                        expression_total=4, grammar_total=2,
                        grammar_filler_ratio=0.0)
        row_a = dict(self.records(["a"], ["b"])[0], scores=scores_a)
        row_b = dict(self.records(["a"], ["b"])[0], scores=scores_b)
        comparison = build_comparison([row_a], [row_b])
        summary = comparison["summary"]
        self.assertAlmostEqual(summary["expression_precision"]["delta"], 0.1, places=2)
        self.assertAlmostEqual(summary["expression_recall"]["delta"], -0.1, places=2)
        self.assertAlmostEqual(summary["grammar_filler_ratio"]["delta"], -0.5, places=2)
        markdown = render_markdown(comparison)
        self.assertIn("为什么是这个分数", markdown)
        self.assertIn("precision", markdown)


class ResultPersistenceTests(unittest.TestCase):
    """结果保存：prompt_version / provider 必须能落库并读回（A/B 报告靠它归因）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = FactoryStore(Path(self.temp.name) / "bench.db")

    def tearDown(self):
        self.temp.cleanup()

    def test_enrichment_records_which_prompt_produced_it(self):
        item_id = self.store.upsert_item("bench-1", title="t")
        self.store.save_enrichment(item_id, {"topic": "AI 科技"}, model="deepseek-chat",
                                   provider="DeepSeek", prompt_version="ai-enrichment-v2")
        row = self.store.enrichment(item_id)
        self.assertEqual(row["prompt_version"], "ai-enrichment-v2")
        self.assertEqual(row["provider"], "DeepSeek")
        self.assertEqual(row["model"], "deepseek-chat")
        self.assertTrue(row["analyzed_at"])

    def test_empty_arrays_round_trip_as_empty_lists(self):
        item_id = self.store.upsert_item("bench-2", title="t")
        self.store.save_enrichment(item_id, {"topic": "日常口语", "expressions": [],
                                             "grammar_points": [], "key_sentences": []})
        row = self.store.enrichment(item_id)
        self.assertEqual(row["expressions"], [])
        self.assertEqual(row["grammar_points"], [])
        self.assertEqual(row["key_sentences"], [])

    def test_old_database_gets_the_new_columns_added(self):
        """用户本机已有旧库：CREATE TABLE IF NOT EXISTS 不会补列，必须显式迁移。

        旧库 fixture 是**从权威 SCHEMA 反向构造**的（去掉本阶段新增的两列），
        而不是手抄一份表结构 —— 手抄的那份会随着 schema 演进悄悄过期，
        测试就变成在验证一个不存在的历史版本。
        """
        import re
        import sqlite3

        path = Path(self.temp.name) / "legacy.db"
        legacy_schema = re.sub(r"CREATE (UNIQUE )?INDEX[^;]+;", "", SCHEMA)
        for column in ("prompt_version", "provider"):
            legacy_schema = re.sub(rf"^\s*{column}\s+TEXT DEFAULT '',\n", "",
                                   legacy_schema, flags=re.M)
        self.assertNotIn("prompt_version", legacy_schema, "fixture 构造失败：新列没被去掉")

        connection = sqlite3.connect(str(path))
        connection.executescript(legacy_schema)
        connection.execute("INSERT INTO content_items (id, title) VALUES ('old', '旧库内容')")
        connection.execute(
            "INSERT INTO ai_enrichments (id, content_item_id, topic) VALUES ('e', 'old', '旧主题')")
        connection.commit()
        connection.close()

        store = FactoryStore(path)
        store.init()
        with closing(store.connect()) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(ai_enrichments)")}
        self.assertIn("prompt_version", columns)
        self.assertIn("provider", columns)
        self.assertEqual(store.enrichment("old")["topic"], "旧主题", "迁移不能丢老数据")
        store.init()          # 幂等：再跑一次不报错

    def test_saving_after_migration_works(self):
        """迁移完必须能正常写入新列 —— 否则报错会推迟到用户点"重新分析"时才出现。"""
        import re
        import sqlite3

        path = Path(self.temp.name) / "legacy2.db"
        legacy_schema = re.sub(r"CREATE (UNIQUE )?INDEX[^;]+;", "", SCHEMA)
        for column in ("prompt_version", "provider"):
            legacy_schema = re.sub(rf"^\s*{column}\s+TEXT DEFAULT '',\n", "",
                                   legacy_schema, flags=re.M)
        connection = sqlite3.connect(str(path))
        connection.executescript(legacy_schema)
        connection.commit()
        connection.close()

        store = FactoryStore(path)
        item_id = store.upsert_item("after-migration", title="t")
        store.save_enrichment(item_id, {"topic": "AI 科技"}, model="m",
                              provider="DeepSeek", prompt_version="ai-enrichment-v2")
        self.assertEqual(store.enrichment(item_id)["prompt_version"], "ai-enrichment-v2")


if __name__ == "__main__":
    unittest.main()
