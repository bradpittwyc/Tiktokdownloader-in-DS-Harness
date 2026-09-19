"""语法点标注：提示词、章节抽取、.md 落盘。

需求是在「英文学习文档」里**再加一个勾选项**：下载后除了 .docx，
再生成一份**语法点标注的 .md**。

三件事值得盯：

1. **章节清单必须跟着勾选项走**。原来是写死的六节，于是取消「中文对照」之后
   提示词一边说"不需要中文翻译"、一边又要求输出 `## 中文对照` ——
   自相矛盾的指令，模型只能猜。
2. 语法点章节**抠不出来就不写文件**，不留空壳。
3. 写 .md **不额外调用模型**（返回的本来就是 Markdown），也不能因为
   写 .md 失败就把已经生成好的 .docx 判成失败。

全部离线，模型调用打桩。
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
from web_app import Api, extract_section  # noqa: E402


TITLE = "How to Make Pasta"
STAMP = "20260905"

MODEL_REPLY = """# How to Make Pasta

## 视频主题
介绍意面的基本做法。

## 英文原文
First, boil the water. Then add salt.

## 中文对照
先把水烧开。然后加盐。

## 重点词汇
- **boil** /bɔɪl/ v. 煮沸

## 句型与表达
First, ... 用于引出第一步。

## 语法点
**1. First, + 祈使句**
- 原句：First, boil the water.
- 结构：First（副词）+ 祈使句
- 说明：用 First 引出第一步
- 仿写：First, heat the oil.

**2. then 的衔接用法**
- 原句：Then add salt.
- 说明：then 表示接着发生

## 跟读练习
First, boil the water.
"""


class ExtractSectionTests(unittest.TestCase):
    def test_it_pulls_out_the_grammar_section_with_its_heading(self):
        section = extract_section(MODEL_REPLY, "语法点")
        self.assertTrue(section.startswith("## 语法点"))
        self.assertIn("First, + 祈使句", section)
        self.assertIn("仿写：First, heat the oil.", section)

    def test_it_stops_at_the_next_heading(self):
        # 不能把后面的章节一起吞进来
        self.assertNotIn("跟读练习", extract_section(MODEL_REPLY, "语法点"))
        self.assertNotIn("跟读练习", extract_section(MODEL_REPLY, "重点词汇"))

    def test_a_missing_section_is_empty_not_an_error(self):
        self.assertEqual(extract_section(MODEL_REPLY, "不存在的章节"), "")
        self.assertEqual(extract_section("", "语法点"), "")
        self.assertEqual(extract_section(None, "语法点"), "")

    def test_a_section_at_the_end_of_the_document(self):
        self.assertEqual(extract_section("## 语法点\n只有这一节", "语法点"), "## 语法点\n只有这一节")

    def test_it_does_not_confuse_a_substring_heading(self):
        # "## 语法点练习" 不是 "## 语法点"
        document = "## 语法点练习\n不该被抽到\n\n## 语法点\n这个才对"
        self.assertIn("这个才对", extract_section(document, "语法点"))
        self.assertNotIn("不该被抽到", extract_section(document, "语法点"))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_grammar_defaults_to_on(self):
        self.assertTrue(Api().get_learning_options()["grammar"])

    def test_it_round_trips(self):
        api = Api()
        api.set_learning_options({**api.get_learning_options(), "grammar": False})
        self.assertFalse(api.get_learning_options()["grammar"])
        self.assertFalse(json.loads(api._learning_file().read_text(encoding="utf-8"))["grammar"])
        self.assertFalse(Api().get_learning_options()["grammar"], "要能存下来")


class PromptTests(unittest.TestCase):
    """提示词的章节清单必须跟勾选项一致。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.api._learning.update({"enabled": True, "api_key": "k", "translation": True,
                                   "vocabulary": True, "timestamps": True, "grammar": True})
        self.folder = Path(self.temp.name) / "@owner"
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / f"{TITLE}_{STAMP}.mp4").write_bytes(b"v")
        (self.folder / f"{TITLE}_{STAMP}.en.vtt").write_text(
            "WEBVTT\n\n00:00.000 --> 00:02.000\nFirst, boil the water.\n", encoding="utf-8")
        self.item = {"id": "111", "url": "https://www.tiktok.com/@owner/video/111",
                     "title": TITLE, "upload_date": STAMP}

    def run_generation(self, reply=MODEL_REPLY, **flags):
        self.api._learning.update(flags)
        prompts = []

        def fake(prompt, max_tokens=6000):
            prompts.append(prompt)
            return reply

        with patch.object(self.api, "_call_learning_model", side_effect=fake):
            self.api._generate_learning_document(
                self.item, str(self.folder),
                [str(self.folder / f"{TITLE}_{STAMP}.en.vtt")])
        return prompts[0] if prompts else ""

    def test_grammar_is_requested_when_checked(self):
        prompt = self.run_generation(grammar=True)
        self.assertIn("## 语法点", prompt)
        self.assertIn("标注语法点", prompt)
        self.assertIn("仿写", prompt)

    def test_grammar_is_not_requested_when_unchecked(self):
        prompt = self.run_generation(grammar=False)
        self.assertNotIn("## 语法点", prompt)

    def test_unchecking_translation_removes_the_section(self):
        # 这是这次顺带修掉的旧毛病：章节清单以前写死，取消勾选也照样要求输出
        prompt = self.run_generation(translation=False)
        self.assertIn("不需要中文翻译", prompt)
        self.assertNotIn("## 中文对照", prompt, "勾掉了就不该再要求这一节")
        self.assertNotIn("中文对照", prompt, "排版要求里也不该再提它")
        self.assertIn("英文原文按自然段连续书写", prompt)

    def test_unchecking_vocabulary_removes_the_section(self):
        prompt = self.run_generation(vocabulary=False)
        self.assertNotIn("## 重点词汇", prompt)

    def test_the_kept_sections_are_still_there(self):
        prompt = self.run_generation(translation=False, vocabulary=False)
        for heading in ("## 视频主题", "## 英文原文", "## 句型与表达", "## 跟读练习"):
            self.assertIn(heading, prompt)


class GrammarFileTests(unittest.TestCase):
    """生成物：.docx 仍然有，勾了语法点再落一份 .grammar.md。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.api = Api()
        self.api._learning.update({"enabled": True, "api_key": "k", "grammar": True})
        self.events = []
        self.api._emit = lambda function, value: self.events.append((function, value))
        self.folder = Path(self.temp.name) / "@owner"
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / f"{TITLE}_{STAMP}.mp4").write_bytes(b"v")
        self.subtitle = self.folder / f"{TITLE}_{STAMP}.en.vtt"
        self.subtitle.write_text("WEBVTT\n\n00:00.000 --> 00:02.000\nFirst, boil the water.\n",
                                 encoding="utf-8")
        self.item = {"id": "111", "url": "https://www.tiktok.com/@owner/video/111",
                     "title": TITLE, "upload_date": STAMP}

    def generate(self, reply=MODEL_REPLY, **flags):
        self.api._learning.update(flags)
        with patch.object(self.api, "_call_learning_model", return_value=reply):
            self.api._generate_learning_document(self.item, str(self.folder), [str(self.subtitle)])
        return self.folder / f"{TITLE}_{STAMP}.grammar.md", self.folder / f"{TITLE}_{STAMP}.docx"

    def done_event(self):
        return next(value for function, value in self.events
                    if function == "learningProgress" and value.get("state") == "done")

    def test_the_docx_is_still_written(self):
        _, docx = self.generate()
        self.assertTrue(docx.is_file())

    def test_the_grammar_markdown_is_written_next_to_it(self):
        markdown, _ = self.generate()
        self.assertTrue(markdown.is_file())
        text = markdown.read_text(encoding="utf-8")
        self.assertIn("语法点", text)
        self.assertIn("First, + 祈使句", text)
        self.assertIn("仿写：First, heat the oil.", text)

    def test_the_file_has_a_title_and_only_the_grammar_part(self):
        markdown, _ = self.generate()
        text = markdown.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# How to Make Pasta"), text[:60])
        self.assertNotIn("## 重点词汇", text, "只要语法点，不要把整份文档塞进来")
        self.assertNotIn("## 跟读练习", text)

    def test_the_done_event_points_at_both_files(self):
        markdown, docx = self.generate()
        event = self.done_event()
        self.assertEqual(event["path"], str(docx))
        self.assertEqual(event["grammarPath"], str(markdown))
        self.assertIn("语法点", event["message"])

    def test_the_markdown_is_utf8_without_a_bom(self):
        # 带 BOM 的话，有些编辑器/脚本会把 BOM 当成正文的第一个字符
        markdown, _ = self.generate()
        raw = markdown.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "不要 BOM")
        self.assertIn("语法点", raw.decode("utf-8"))

    def test_the_header_falls_back_when_the_model_gives_no_h1(self):
        # 模型没给 `# 标题` 时用固定表头，不能生成一个以 —— 开头的残句
        markdown, _ = self.generate(reply="## 语法点\n- 原句：Boil the water.\n")
        self.assertTrue(markdown.is_file())
        self.assertTrue(markdown.read_text(encoding="utf-8").startswith("# 语法点"))

    def test_no_markdown_when_the_option_is_off(self):
        markdown, docx = self.generate(grammar=False)
        self.assertFalse(markdown.exists())
        self.assertTrue(docx.is_file(), "关掉语法点不影响 docx")
        self.assertEqual(self.done_event()["grammarPath"], "")

    def test_no_markdown_when_the_model_omits_the_section(self):
        # 抠不出章节就什么都不写 —— 不留一个空壳让人以为"生成了但没内容"
        markdown, docx = self.generate(reply="# 标题\n\n## 视频主题\n没有语法点这一节")
        self.assertFalse(markdown.exists())
        self.assertTrue(docx.is_file())
        self.assertEqual(self.done_event()["grammarPath"], "")

    def test_a_failure_writing_the_markdown_does_not_fail_the_docx(self):
        with patch.object(Api, "_write_grammar_markdown",
                          side_effect=OSError("磁盘满了")):
            markdown, docx = self.generate()
        self.assertTrue(docx.is_file())
        event = self.done_event()
        self.assertEqual(event["grammarPath"], "")
        self.assertEqual(self.errors_count(), 0)

    def errors_count(self):
        return len([value for function, value in self.events
                    if function == "learningProgress" and value.get("state") == "failed"])


if __name__ == "__main__":
    unittest.main()
