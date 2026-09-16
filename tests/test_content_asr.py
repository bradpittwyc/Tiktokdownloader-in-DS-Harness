"""ASR / Transcript Core 的回归网。

锁住的是这一条闭环（也是本分支的验收项）：

    本地视频 / 音频 → 检查已有字幕 → 有字幕就直接读 → 没有才进 ASR
    → 得到 transcript → 写回 ContentItem / Store → 更新 transcript_status
    → 失败给出结构化错误 + 可重试

覆盖：
- 已有字幕（记着的字幕文件 / 媒体旁边的同名字幕）
- 无字幕 → ASR 成功
- 无字幕 → ASR 失败（后端没装 / 识别过程报错 / 没有媒体文件）
- retry（失败后救回来；done 的内容默认不重跑）
- 重启后 transcript 仍然在（文本 + 状态 + 来源）
- 四种来源明确区分：subtitle / local_asr / external_asr / failed+retry
- 外部 provider 是**接口 + 真实 HTTP 调用**，但今天没有可用 API：
  没配置时必须明确报错，绝不允许伪造转写结果

全部离线：ASR 后端、外部 HTTP 都是测试替身；没有安装 faster-whisper 也能跑。
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_factory.asr import (  # noqa: E402
    ASR_MODE_LOCAL, ASR_MODE_OFF, ExternalAsrProvider, LocalAsrProvider,
    ProviderOutput, STATUS_DONE, STATUS_FAILED, STATUS_RUNNING, SubtitleProvider,
    TranscriptError, TranscriptErrorCode, TranscriptProvider, TranscriptResult,
    TranscriptRunLog, TranscriptService, TranscriptSource, choose_error,
    default_providers,)
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.pipeline import ContentPipeline  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402

SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello from the subtitle file.\n"

# 测试替身（**不是**真实识别结果）：真的 faster-whisper 本机没装，
# 装上之后走的是同一条 LocalAsrProvider 代码路径。
FAKE_ASR_TEXT = "this text came from the fake local asr backend"

# 闭环用例用的模型回复（与 test_content_factory.py 里那份同源，这里自带一份，
# 避免测试模块之间互相 import 造成的发现顺序依赖）。
GOOD_REPLY = json.dumps({
    "topic": "AI 科技", "subtopic": "AI 与就业", "cefr_level": "b2", "accent": "美音",
    "speech_speed": "偏快", "learning_value": 0.92, "keywords": ["collapse"],
    "expressions": ["the cost of thinking is collapsing"],
    "grammar_points": ["whether 引导宾语从句"],
    "key_sentences": [{"text": "I see a tool.", "translation_zh": "我看到一个工具。"}],
    "summary_zh": "摘要", "recommended_task": "复述",
}, ensure_ascii=False)


class FakeModelHttp:
    """假的模型 HTTP（只给闭环用例用）。"""

    def post(self, url, headers=None, json=None, timeout=None):
        class _Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": GOOD_REPLY}}]}
        return _Response()


class FakeTranscriber:
    """假的本地 ASR：可返回文本、可抛异常、记录被调用的媒体路径。"""

    def __init__(self, text=FAKE_ASR_TEXT, error=None):
        self.text = text
        self.error = error
        self.calls = []

    def __call__(self, media, **kwargs):
        self.calls.append({"media": str(media), "kwargs": kwargs})
        if self.error is not None:
            raise self.error
        return self.text


class FakeHttpResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHttp:
    """假的外部 ASR HTTP：只验证「请求真的发出去了、回来了怎么解析」。"""

    def __init__(self, payload=None, error=None):
        self.payload = {"text": "external asr text"} if payload is None else payload
        self.error = error
        self.calls = []

    def post(self, url, headers=None, files=None, data=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "data": data})
        if self.error is not None:
            raise self.error
        return FakeHttpResponse(self.payload)


class FakeExternalProvider(TranscriptProvider):
    """注入用的外部 provider 替身：证明「外部来源接进链里」这条路是通的。"""

    name = TranscriptSource.EXTERNAL_ASR
    kind = TranscriptSource.EXTERNAL_ASR
    label = "假的外部识别服务"
    start_message = "正在调用外部识别服务…"

    def __init__(self, text="external provider text"):
        self.text = text
        self.calls = 0

    def fetch(self, item):
        self.calls += 1
        return ProviderOutput(text=self.text, asset=item.get("local_video_path") or "")


class AsrEnvCase(unittest.TestCase):
    """每个用例一个临时 LOCALAPPDATA + 真 sqlite（和线上同一套 store）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.store = FactoryStore(self.root / "content-factory.db")
        self.settings = FactorySettings(self.root)
        self.events = []

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    # ---- 便捷构造 ------------------------------------------------------
    def media(self, name="clip.mp4", folder="videos", content=b"fake media bytes"):
        target = self.root / folder
        target.mkdir(parents=True, exist_ok=True)
        path = target / name
        path.write_bytes(content)
        return path

    def subtitle(self, name="clip.en.srt", folder="videos", text=SRT):
        target = self.root / folder
        target.mkdir(parents=True, exist_ok=True)
        path = target / name
        path.write_text(text, encoding="utf-8")
        return path

    def item(self, video_id="v1", **fields):
        return self.store.upsert_item(video_id, title="测试内容", **fields)

    def service(self, transcriber=None, providers=None, probe=None, run_log=None, http=None):
        self.transcriber = transcriber if transcriber is not None else FakeTranscriber()
        return TranscriptService(
            self.store, self.settings, providers=providers,
            emit=lambda event: self.events.append(event),
            run_log=run_log, transcriber=self.transcriber,
            asr_probe=probe if probe is not None else (lambda: True), http=http)

    def states(self):
        return [event["state"] for event in self.events]

    def messages(self):
        return [event["message"] for event in self.events]


class ExistingSubtitleTests(AsrEnvCase):
    """来源 1：已有字幕直接读取 —— 零成本、最准，永远排第一。"""

    def test_recorded_subtitle_file_is_used_without_touching_asr(self):
        item_id = self.item(local_subtitle_path=str(self.subtitle()))
        result = self.service().transcribe(item_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.source, TranscriptSource.SUBTITLE)
        self.assertEqual(result.status, STATUS_DONE)
        self.assertEqual(result.text, "Hello from the subtitle file.")
        self.assertEqual(self.transcriber.calls, [], "有字幕就不该去跑 ASR")
        item = self.store.item(item_id)
        self.assertEqual(item["transcript_status"], "done")
        self.assertEqual(item["transcript_text"], "Hello from the subtitle file.")
        self.assertEqual(item["last_error"], "")

    def test_subtitle_next_to_the_media_is_found_and_written_back(self):
        media = self.media()
        self.subtitle()
        item_id = self.item(local_video_path=str(media))
        self.assertEqual(self.store.item(item_id)["local_subtitle_path"], "")
        result = self.service().transcribe(item_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.source, TranscriptSource.SUBTITLE)
        stored = self.store.item(item_id)["local_subtitle_path"]
        self.assertTrue(stored.endswith("clip.en.srt"), stored)
        self.assertEqual(self.transcriber.calls, [])

    def test_subtitle_cleaning_is_reused(self):
        """字幕里的序号 / 时间轴 / 音乐标记不能进 transcript（复用 transcript.py）。"""
        messy = ("1\n00:00:01,000 --> 00:00:03,000\n<i>Hello</i> there\n\n"
                 "2\n00:00:03,000 --> 00:00:05,000\n[Music] Hello there\n")
        item_id = self.item(local_subtitle_path=str(self.subtitle(text=messy)))
        result = self.service().transcribe(item_id)
        self.assertEqual(result.text, "Hello there")
        self.assertNotIn("-->", result.text)

    def test_transcript_already_in_the_store_is_not_recomputed(self):
        item_id = self.item(local_video_path=str(self.media()))
        self.store.update_item(item_id, transcript_text="已有的文本", transcript_status="done")
        result = self.service().transcribe(item_id)
        self.assertTrue(result.ok)
        self.assertTrue(result.cached)
        self.assertEqual(result.text, "已有的文本")
        self.assertEqual(self.transcriber.calls, [], "已有文本不该重跑")
        self.assertEqual(self.store.item(item_id)["transcript_text"], "已有的文本")


class MissingSubtitleTests(AsrEnvCase):
    """没有字幕时才进 ASR，并且要能明确区分「没字幕」和「字幕坏了」。"""

    def test_no_subtitle_falls_through_to_local_asr(self):
        item_id = self.item(local_video_path=str(self.media()))
        result = self.service().transcribe(item_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.source, TranscriptSource.LOCAL_ASR)
        self.assertEqual(result.text, FAKE_ASR_TEXT)
        self.assertEqual(len(self.transcriber.calls), 1)

    def test_extracted_audio_is_preferred_over_the_video(self):
        video = self.media()
        audio = self.media(name="clip.mp3", content=b"fake audio bytes")
        item_id = self.item(local_video_path=str(video), local_audio_path=str(audio))
        self.service().transcribe(item_id)
        self.assertEqual(self.transcriber.calls[0]["media"], str(audio),
                         "提取好的音频优先喂给 ASR（更快，结果一样）")

    def test_unreadable_subtitle_is_reported_distinctly_and_falls_through(self):
        """字幕文件是空的 → 不是「没字幕」而是「字幕读不出来」，但仍然继续试 ASR。"""
        empty = self.subtitle(text="")
        media = self.media()
        item_id = self.item(local_video_path=str(media), local_subtitle_path=str(empty))
        result = self.service().transcribe(item_id)
        self.assertTrue(result.ok, "字幕坏了不该挡住 ASR")
        self.assertEqual(result.source, TranscriptSource.LOCAL_ASR)

    def test_unreadable_subtitle_without_asr_reports_the_subtitle(self):
        empty = self.subtitle(text="")
        item_id = self.item(local_subtitle_path=str(empty))
        service = self.service(providers=[SubtitleProvider()])
        result = service.transcribe(item_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.SUBTITLE_UNREADABLE)
        self.assertTrue(result.error.retryable)

    def test_no_subtitle_and_no_media_reports_the_missing_media(self):
        item_id = self.item()
        result = self.service().transcribe(item_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.MEDIA_MISSING,
                         "连素材都没有时，报「外部服务没配置」是误导")
        self.assertEqual(self.store.item(item_id)["transcript_status"], "failed")


class LocalAsrSuccessTests(AsrEnvCase):
    """来源 2：本地 ASR 成功 —— 文本真的写回内容库。"""

    def test_local_asr_success_is_written_back(self):
        item_id = self.item(local_video_path=str(self.media()))
        result = self.service().transcribe(item_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.source, TranscriptSource.LOCAL_ASR)
        self.assertEqual(result.provider, TranscriptSource.LOCAL_ASR)
        self.assertEqual(result.status, STATUS_DONE)
        self.assertEqual(result.chars, len(FAKE_ASR_TEXT))
        item = self.store.item(item_id)
        self.assertEqual(item["transcript_text"], FAKE_ASR_TEXT)
        self.assertEqual(item["transcript_status"], "done")
        self.assertEqual(item["last_error"], "")

    def test_progress_events_describe_the_transcript_stage(self):
        item_id = self.item(local_video_path=str(self.media()))
        self.service().transcribe(item_id)
        self.assertIn(STATUS_RUNNING, self.states())
        self.assertTrue(all(event["stage"] == "transcript" for event in self.events))
        self.assertIn("正在语音识别（ASR）…", self.messages())
        self.assertTrue(any("语音识别完成" in message for message in self.messages()))

    def test_empty_asr_output_is_a_failure_not_a_silent_success(self):
        item_id = self.item(local_video_path=str(self.media()))
        result = self.service(transcriber=FakeTranscriber(text="   ")).transcribe(item_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.EMPTY_RESULT)
        self.assertEqual(self.store.item(item_id)["transcript_text"], "")


class LocalAsrFailureTests(AsrEnvCase):
    """来源 4 的前半：失败必须结构化、必须写进 last_error、必须说明能不能重试。"""

    def test_backend_unavailable_is_explicit(self):
        item_id = self.item(local_video_path=str(self.media()))
        service = self.service(probe=lambda: False)
        result = service.transcribe(item_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.BACKEND_UNAVAILABLE)
        self.assertTrue(result.error.retryable)
        self.assertIn("语音识别", result.error.message)
        row = self.store.item(item_id)
        self.assertEqual(row["transcript_status"], "failed")
        self.assertEqual(row["last_error"], result.error.message)

    def test_backend_crash_is_wrapped_with_a_code(self):
        from content_factory.transcript import ASRUnavailable
        item_id = self.item(local_video_path=str(self.media()))
        service = self.service(transcriber=FakeTranscriber(
            error=ASRUnavailable("语音识别失败：RuntimeError: boom")))
        result = service.transcribe(item_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.ASR_FAILED)
        self.assertIn("boom", result.error.message)
        self.assertEqual(self.store.item(item_id)["transcript_status"], "failed")

    def test_unknown_provider_exception_becomes_a_structured_error(self):
        item_id = self.item(local_video_path=str(self.media()))
        service = self.service(transcriber=FakeTranscriber(error=RuntimeError("segfault")))
        result = service.transcribe(item_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.ASR_FAILED)
        self.assertIn("segfault", result.error.message)

    def test_missing_media_is_reported_as_such(self):
        item_id = self.item(local_video_path=str(self.root / "videos" / "gone.mp4"))
        result = self.service().transcribe(item_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.MEDIA_MISSING)
        self.assertIn("记录的文件不存在", result.error.detail)

    def test_disabled_asr_does_not_run_and_says_so(self):
        item_id = self.item(local_video_path=str(self.media()))
        result = self.service().transcribe(item_id, allow_asr=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.ASR_DISABLED)
        self.assertFalse(result.error.retryable)
        self.assertEqual(self.transcriber.calls, [])

    def test_disabled_asr_still_reads_an_existing_subtitle(self):
        item_id = self.item(local_video_path=str(self.media()),
                            local_subtitle_path=str(self.subtitle()))
        result = self.service().transcribe(item_id, allow_asr=False)
        self.assertTrue(result.ok, "关掉 ASR 不等于放弃本来就有字幕的内容")
        self.assertEqual(result.source, TranscriptSource.SUBTITLE)

    def test_failure_event_carries_the_error_code(self):
        item_id = self.item(local_video_path=str(self.media()))
        self.service(probe=lambda: False).transcribe(item_id)
        failed = [event for event in self.events if event["state"] == STATUS_FAILED]
        self.assertTrue(failed, self.events)
        self.assertEqual(failed[-1]["error"]["code"], TranscriptErrorCode.BACKEND_UNAVAILABLE)

    def test_unknown_item_does_not_raise(self):
        result = self.service().transcribe("item_missing")
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.ITEM_MISSING)
        self.assertFalse(result.error.retryable)


class RetryTests(AsrEnvCase):
    """来源 4 的后半：失败可重试，而且重试成功之后失败痕迹要清干净。"""

    def test_retry_after_installing_the_backend_succeeds(self):
        item_id = self.item(local_video_path=str(self.media()))
        broken = self.service(probe=lambda: False)
        self.assertFalse(broken.transcribe(item_id).ok)
        self.assertEqual(self.store.item(item_id)["transcript_status"], "failed")
        self.assertIn("语音识别", self.store.item(item_id)["last_error"])

        fixed = self.service(transcriber=FakeTranscriber(), probe=lambda: True)
        result = fixed.retry(item_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertTrue(result.retried)
        self.assertEqual(result.text, FAKE_ASR_TEXT)
        row = self.store.item(item_id)
        self.assertEqual(row["transcript_status"], "done")
        self.assertEqual(row["last_error"], "", "重试成功后不能留着旧错误")

    def test_retry_does_not_rerun_a_finished_item(self):
        item_id = self.item(local_video_path=str(self.media()))
        service = self.service()
        self.assertTrue(service.transcribe(item_id).ok)
        result = service.retry(item_id)
        self.assertTrue(result.ok)
        self.assertTrue(result.cached)
        self.assertEqual(len(self.transcriber.calls), 1, "done 的内容默认不该重跑")

    def test_forced_retry_reruns_the_chain(self):
        item_id = self.item(local_video_path=str(self.media()))
        service = self.service()
        service.transcribe(item_id)
        result = service.retry(item_id, force=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.retried)
        self.assertEqual(len(self.transcriber.calls), 2, "force=True 必须真的重跑")

    def test_retry_failed_batch_only_touches_failed_items(self):
        good = self.item("ok-1", local_subtitle_path=str(self.subtitle()))
        bad = self.item("bad-1", local_video_path=str(self.media(name="b.mp4")))
        self.service().transcribe(good)                       # 成功
        service = self.service(probe=lambda: False)
        service.transcribe(bad)                               # 失败
        fixed = self.service(transcriber=FakeTranscriber(), probe=lambda: True)
        summary = fixed.retry_failed()
        self.assertEqual(summary["retried"], 1, summary)
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(self.store.item(bad)["transcript_status"], "done")
        self.assertEqual(self.store.item(good)["transcript_status"], "done")


class PersistenceTests(AsrEnvCase):
    """重启（新建 store / 新建 service）= 重新读同一个 sqlite。"""

    def test_transcript_survives_a_restart(self):
        item_id = self.item(local_video_path=str(self.media()))
        self.service().transcribe(item_id)

        reopened = FactoryStore(self.root / "content-factory.db")
        restarted = TranscriptService(reopened, FactorySettings(self.root),
                                      transcriber=FakeTranscriber(),
                                      asr_probe=lambda: True)
        row = reopened.item(item_id)
        self.assertEqual(row["transcript_text"], FAKE_ASR_TEXT)
        self.assertEqual(row["transcript_status"], "done")
        state = restarted.status(item_id)
        self.assertTrue(state["hasText"])
        self.assertEqual(state["status"], STATUS_DONE)
        self.assertEqual(state["source"], TranscriptSource.LOCAL_ASR,
                         "重启后仍然知道这条文本是 ASR 转出来的")

    def test_failure_and_its_code_survive_a_restart(self):
        item_id = self.item(local_video_path=str(self.media()))
        self.service(probe=lambda: False).transcribe(item_id)

        reopened = FactoryStore(self.root / "content-factory.db")
        restarted = TranscriptService(reopened, FactorySettings(self.root),
                                      asr_probe=lambda: False)
        state = restarted.status(item_id)
        self.assertEqual(state["status"], STATUS_FAILED)
        self.assertEqual(state["error"]["code"], TranscriptErrorCode.BACKEND_UNAVAILABLE)
        self.assertTrue(state["error"]["retryable"])

    def test_restart_does_not_lose_the_subtitle_asset_path(self):
        media = self.media()
        self.subtitle()
        item_id = self.item(local_video_path=str(media))
        self.service().transcribe(item_id)
        reopened = FactoryStore(self.root / "content-factory.db")
        state = TranscriptService(reopened, FactorySettings(self.root)).status(item_id)
        self.assertTrue(state["subtitlePath"].endswith("clip.en.srt"))


class ExternalProviderTests(AsrEnvCase):
    """来源 3：外部 ASR —— 接口是真的，今天没有可用 API，所以绝不假装成功。"""

    def test_unconfigured_external_provider_is_not_available(self):
        provider = ExternalAsrProvider()
        self.assertFalse(provider.available())
        self.assertIn("未配置", provider.detail())
        with self.assertRaises(TranscriptError) as ctx:
            provider.fetch({"local_video_path": str(self.media())})
        self.assertEqual(ctx.exception.code, TranscriptErrorCode.PROVIDER_UNCONFIGURED)
        self.assertTrue(ctx.exception.retryable)

    def test_local_backend_missing_outranks_the_unconfigured_external_service(self):
        """本机后端没装 + 外部服务没配置：报更根本、更可操作的那条。"""
        item_id = self.item(local_video_path=str(self.media()))
        result = self.service(probe=lambda: False).transcribe(item_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.BACKEND_UNAVAILABLE)

    def test_external_provider_speaks_http_when_configured(self):
        http = FakeHttp({"text": "hello from a real http asr endpoint"})
        provider = ExternalAsrProvider(api_base="https://asr.example.com/v1",
                                       api_key="sk-test", http=http)
        self.assertTrue(provider.available())
        output = provider.fetch({"local_video_path": str(self.media())})
        self.assertEqual(output.text, "hello from a real http asr endpoint")
        self.assertEqual(http.calls[0]["url"], "https://asr.example.com/v1/audio/transcriptions")
        self.assertEqual(http.calls[0]["data"]["model"], "whisper-1")
        self.assertIn("Bearer sk-test", http.calls[0]["headers"]["Authorization"])

    def test_external_http_failure_becomes_a_structured_error(self):
        provider = ExternalAsrProvider(api_base="https://asr.example.com/v1",
                                       api_key="sk-test", http=FakeHttp(error=RuntimeError("502")))
        with self.assertRaises(TranscriptError) as ctx:
            provider.fetch({"local_video_path": str(self.media())})
        self.assertEqual(ctx.exception.code, TranscriptErrorCode.ASR_FAILED)
        self.assertIn("502", ctx.exception.message)

    def test_external_provider_reads_its_configuration_from_settings(self):
        """外部服务的配置项都在 ai 分区里，但没有写进公共默认值（不是公共 contract）。"""
        with tempfile.TemporaryDirectory() as folder:
            settings = FactorySettings(Path(folder))
            settings.update("ai", {"asr_api_base": "https://asr.example.com/v1/",
                                   "asr_api_key": "sk-test",
                                   "asr_external_model": "whisper-large-v3",
                                   "asr_language": "en",
                                   "asr_model": "small"})
            provider = ExternalAsrProvider.from_settings(settings)
            self.assertTrue(provider.available())
            self.assertEqual(provider.model, "whisper-large-v3",
                             "asr_model 是本机模型大小，不能和外部服务的模型名混用")
            self.assertEqual(provider.language, "en")
            self.assertEqual(provider.endpoint(),
                             "https://asr.example.com/v1/audio/transcriptions")
            self.assertNotIn("asr_api_key", FactorySettings(Path(folder) / "x").section("ai"))

    def test_external_provider_can_be_injected_into_the_chain(self):
        """链是开着的：外部 provider 插进来就能真的被用到（来源标记为 external_asr）。"""
        item_id = self.item(local_video_path=str(self.media()))
        external = FakeExternalProvider()
        service = self.service(providers=[SubtitleProvider(),
                                          LocalAsrProvider(probe=lambda: False),
                                          external])
        result = service.transcribe(item_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.source, TranscriptSource.EXTERNAL_ASR)
        self.assertEqual(external.calls, 1)
        self.assertEqual(self.store.item(item_id)["transcript_text"], "external provider text")

    def test_providers_status_reports_every_source(self):
        status = self.service().providers_status()
        self.assertEqual([row["kind"] for row in status],
                         [TranscriptSource.SUBTITLE, TranscriptSource.LOCAL_ASR,
                          TranscriptSource.EXTERNAL_ASR])
        self.assertTrue(status[0]["available"], "读字幕永远可用")
        self.assertIn("detail", status[1])


class AsrModeSettingTests(AsrEnvCase):
    """设置页「ASR / 转写」的三个选项必须真的生效（这个键一直存在，只是一直没人读）。"""

    def test_default_mode_is_the_local_backend(self):
        self.assertEqual(self.service().asr_mode(), ASR_MODE_LOCAL)

    def test_disabled_mode_reports_why_instead_of_running_asr(self):
        self.settings.update("ai", {"asr_provider": "不启用"})
        item_id = self.item(local_video_path=str(self.media()))
        result = self.service().transcribe(item_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.ASR_DISABLED)
        self.assertFalse(result.error.retryable)
        self.assertEqual(self.transcriber.calls, [], "设置里关了 ASR 就不该跑识别")
        self.assertIn("不启用", result.error.message)

    def test_disabled_mode_still_reads_an_existing_subtitle(self):
        self.settings.update("ai", {"asr_provider": "不启用"})
        item_id = self.item(local_subtitle_path=str(self.subtitle()))
        result = self.service().transcribe(item_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.source, TranscriptSource.SUBTITLE)

    def test_external_mode_skips_the_local_backend(self):
        self.settings.update("ai", {"asr_provider": "外部服务"})
        item_id = self.item(local_video_path=str(self.media()))
        service = self.service(probe=lambda: True)
        kinds = [provider.kind for provider in service.chain()]
        self.assertEqual(kinds, [TranscriptSource.SUBTITLE, TranscriptSource.EXTERNAL_ASR])
        result = service.transcribe(item_id)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, TranscriptErrorCode.PROVIDER_UNCONFIGURED,
                         "选了外部服务，就该明确报外部服务没配置")
        self.assertEqual(self.transcriber.calls, [], "既然是外部模式，本机后端不该被调用")

    def test_external_mode_uses_a_configured_external_provider(self):
        self.settings.update("ai", {"asr_provider": "外部服务",
                                    "asr_api_base": "https://asr.example.com/v1",
                                    "asr_api_key": "sk-test"})
        http = FakeHttp({"text": "text from the configured external service"})
        service = self.service(http=http)
        item_id = self.item(local_video_path=str(self.media()))
        result = service.transcribe(item_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.source, TranscriptSource.EXTERNAL_ASR)
        self.assertEqual(result.text, "text from the configured external service")
        self.assertEqual(http.calls[0]["url"],
                         "https://asr.example.com/v1/audio/transcriptions")
        self.assertEqual(self.transcriber.calls, [], "外部模式下本机后端不该被调用")

    def test_local_mode_still_falls_back_to_a_configured_external_service(self):
        self.settings.update("ai", {"asr_api_base": "https://asr.example.com/v1",
                                    "asr_api_key": "sk-test"})
        http = FakeHttp({"text": "fallback external text"})
        service = self.service(probe=lambda: False, http=http)
        item_id = self.item(local_video_path=str(self.media()))
        result = service.transcribe(item_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.source, TranscriptSource.EXTERNAL_ASR)
        self.assertEqual(result.text, "fallback external text")

    def test_providers_status_shows_the_mode(self):
        self.settings.update("ai", {"asr_provider": "不启用"})
        rows = self.service().providers_status()
        self.assertTrue(all(row["mode"] == ASR_MODE_OFF for row in rows))
        enabled = {row["kind"]: row["enabled"] for row in rows}
        self.assertTrue(enabled[TranscriptSource.SUBTITLE])
        self.assertFalse(enabled[TranscriptSource.LOCAL_ASR])
        self.assertFalse(enabled[TranscriptSource.EXTERNAL_ASR])


class RunLogTests(AsrEnvCase):
    """来源信息（字幕 / ASR / 失败码）不能污染公共表结构，但也不能丢。"""

    def test_run_log_records_source_and_error(self):
        item_id = self.item(local_video_path=str(self.media()))
        log = TranscriptRunLog(self.root / "transcripts")
        service = self.service(run_log=log)
        service.transcribe(item_id)
        record = log.load(item_id)
        self.assertEqual(record["source"], TranscriptSource.LOCAL_ASR)
        self.assertEqual(record["status"], STATUS_DONE)
        self.assertEqual(record["chars"], len(FAKE_ASR_TEXT))

    def test_run_log_failure_record_carries_the_code(self):
        item_id = self.item(local_video_path=str(self.media()))
        log = TranscriptRunLog(self.root / "transcripts")
        self.service(probe=lambda: False, run_log=log).transcribe(item_id)
        record = log.load(item_id)
        self.assertEqual(record["status"], STATUS_FAILED)
        self.assertEqual(record["code"], TranscriptErrorCode.BACKEND_UNAVAILABLE)
        self.assertTrue(record["retryable"])

    def test_broken_run_log_does_not_break_transcription(self):
        """运行记录只是附加信息：文件坏了，主链路（文本 + 状态）必须照常。"""
        item_id = self.item(local_video_path=str(self.media()))
        log = TranscriptRunLog(self.root / "transcripts")
        log.path(item_id).parent.mkdir(parents=True, exist_ok=True)
        log.path(item_id).write_text("{ 这不是 json", encoding="utf-8")
        result = self.service(run_log=log).transcribe(item_id)
        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(self.store.item(item_id)["transcript_status"], "done")


class ContractTests(unittest.TestCase):
    """错误码 / 结果 / 状态机这些契约本身的形状。"""

    def test_status_machine_matches_the_content_item_contract(self):
        from content_factory.asr import TRANSCRIPT_STATUSES
        self.assertEqual(TRANSCRIPT_STATUSES, ("pending", "running", "done", "failed"))

    def test_error_codes_are_unique_and_prioritised(self):
        codes = list(TranscriptErrorCode.ALL)
        self.assertEqual(len(codes), len(set(codes)))
        from content_factory.asr.models import ERROR_PRIORITY
        self.assertEqual(set(ERROR_PRIORITY), set(codes), "每个错误码都要有优先级")

    def test_choose_error_prefers_the_actionable_one(self):
        errors = [
            TranscriptError(TranscriptErrorCode.MEDIA_MISSING, "没有媒体文件"),
            TranscriptError(TranscriptErrorCode.BACKEND_UNAVAILABLE, "没装识别后端"),
        ]
        self.assertEqual(choose_error(errors).code, TranscriptErrorCode.BACKEND_UNAVAILABLE)
        self.assertEqual(choose_error([]).code, TranscriptErrorCode.NO_INPUT)

    def test_error_serialises_to_a_structured_dict(self):
        error = TranscriptError(TranscriptErrorCode.ASR_FAILED, "识别挂了",
                                provider="local_asr", hint="重新下载再试")
        payload = error.to_dict()
        self.assertEqual(payload["code"], "asr_failed")
        self.assertEqual(payload["message"], "识别挂了")
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["hint"], "重新下载再试")
        self.assertEqual(str(error), "识别挂了")

    def test_result_serialises_to_json(self):
        result = TranscriptResult(item_id="item_1", ok=True, text="hi", chars=2,
                                  source=TranscriptSource.SUBTITLE, status=STATUS_DONE)
        payload = json.loads(json.dumps(result.to_dict(), ensure_ascii=False))
        self.assertEqual(payload["itemId"], "item_1")
        self.assertEqual(payload["source"], "subtitle")
        self.assertIsNone(payload["error"])
        self.assertTrue(result)

    def test_non_retryable_codes_are_marked(self):
        self.assertFalse(TranscriptError(TranscriptErrorCode.ITEM_MISSING, "没了").retryable)
        self.assertFalse(TranscriptError(TranscriptErrorCode.ASR_DISABLED, "关了").retryable)
        self.assertTrue(TranscriptError(TranscriptErrorCode.ASR_FAILED, "挂了").retryable)

    def test_default_providers_order_is_subtitle_first(self):
        providers = default_providers(None)
        self.assertIsInstance(providers[0], SubtitleProvider)
        self.assertIsInstance(providers[1], LocalAsrProvider)
        self.assertIsInstance(providers[2], ExternalAsrProvider)

    def test_local_asr_provider_reads_optional_settings_without_touching_defaults(self):
        """可选配置从设置里读，但 settings_store.DEFAULTS 不动（那是公共 contract）。"""
        with tempfile.TemporaryDirectory() as folder:
            settings = FactorySettings(Path(folder))
            settings.update("ai", {"asr_model": "small", "asr_language": "en"})
            provider = next(row for row in default_providers(settings)
                            if row.kind == TranscriptSource.LOCAL_ASR)
            self.assertEqual(provider.transcribe_kwargs,
                             {"model_size": "small", "language": "en"})
            defaults = FactorySettings(Path(folder) / "other").section("ai")
            self.assertNotIn("asr_model", defaults, "没有往公共设置默认值里加键")


class PipelineIntegrationTests(AsrEnvCase):
    """pipeline 的转写阶段现在是 ASR Core 的薄封装：旧签名与旧行为都不能变。"""

    def setUp(self):
        super().setUp()
        self.pipeline_events = []
        self.pipeline = ContentPipeline(
            self.store, self.settings,
            emit=lambda name, payload: self.pipeline_events.append((name, payload)))

    def test_ensure_transcript_keeps_the_old_return_shape(self):
        item_id = self.item(local_subtitle_path=str(self.subtitle()))
        ok, text, reason = self.pipeline.ensure_transcript(item_id)
        self.assertTrue(ok)
        self.assertEqual(text, "Hello from the subtitle file.")
        self.assertEqual(reason, "")

    def test_ensure_transcript_failure_returns_a_readable_reason(self):
        item_id = self.item()
        with patch("content_factory.pipeline.asr_available", return_value=False):
            ok, text, reason = self.pipeline.ensure_transcript(item_id)
        self.assertFalse(ok)
        self.assertEqual(text, "")
        self.assertIn("语音识别", reason)
        self.assertEqual(self.store.item(item_id)["transcript_status"], "failed",
                         "失败状态必须落库（旧行为）")

    def test_injected_transcriber_is_still_used(self):
        """ContentPipeline(transcribe=...) 的注入必须继续生效。"""
        item_id = self.item(local_video_path=str(self.media()))
        fake = FakeTranscriber(text="injected transcriber text")
        pipeline = ContentPipeline(self.store, self.settings, transcribe=fake)
        with patch("content_factory.pipeline.asr_available", return_value=True):
            ok, text, _reason = pipeline.ensure_transcript(item_id)
        self.assertTrue(ok)
        self.assertEqual(text, "injected transcriber text")
        self.assertEqual(len(fake.calls), 1)

    def test_stage_events_still_use_enrich_progress(self):
        item_id = self.item(local_video_path=str(self.media()))
        fake = FakeTranscriber()
        pipeline = ContentPipeline(
            self.store, self.settings, transcribe=fake,
            emit=lambda name, payload: self.pipeline_events.append((name, payload)))
        with patch("content_factory.pipeline.asr_available", return_value=True):
            pipeline.ensure_transcript(item_id)
        transcript_events = [payload for name, payload in self.pipeline_events
                             if name == "enrichProgress" and payload.get("stage") == "transcript"]
        self.assertTrue(transcript_events)
        self.assertIn("running", [event["state"] for event in transcript_events])
        self.assertTrue(all(event["stageLabel"] == "转写" for event in transcript_events))

    def test_structured_entry_points_are_available_on_the_pipeline(self):
        item_id = self.item(local_video_path=str(self.media()))
        pipeline = ContentPipeline(self.store, self.settings, transcribe=FakeTranscriber())
        with patch("content_factory.pipeline.asr_available", return_value=True):
            result = pipeline.transcribe_item(item_id)
            self.assertTrue(result.ok, result.to_dict())
            self.assertEqual(result.source, TranscriptSource.LOCAL_ASR)
            state = pipeline.transcribe_status(item_id)
            self.assertEqual(state["status"], STATUS_DONE)
            self.assertTrue(pipeline.retry_transcript(item_id).cached)

    def test_stats_reports_the_provider_chain(self):
        stats = self.pipeline.stats()
        self.assertIn("asrAvailable", stats)
        kinds = [row["kind"] for row in stats["asrProviders"]]
        self.assertEqual(kinds, [TranscriptSource.SUBTITLE, TranscriptSource.LOCAL_ASR,
                                 TranscriptSource.EXTERNAL_ASR])

    def test_enrich_one_still_works_end_to_end_with_a_subtitle(self):
        """闭环没被拆坏：字幕 → transcript → AI 标注 → 落库。"""
        from content_factory.ai_enrichment import EnrichmentService
        self.settings.update("ai", {"api_key": "sk-test", "model": "deepseek-chat"})
        self.pipeline._enricher = EnrichmentService(self.settings, http=FakeModelHttp())
        folder = self.root / "closure"
        folder.mkdir()
        (folder / "clip.mp4").write_bytes(b"x")
        (folder / "clip.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nAI is changing how we think about work.\n",
            encoding="utf-8")
        item_id = self.pipeline.create_from_local(folder)["ids"][0]
        result = self.pipeline.enrich_one(item_id)
        self.assertTrue(result["ok"], result)
        view = self.store.item_view(item_id)
        self.assertEqual(view["transcript_status"], "done")
        self.assertEqual(view["ai_status"], "done")
        self.assertIn("AI is changing", view["transcript_text"])


if __name__ == "__main__":
    unittest.main()
