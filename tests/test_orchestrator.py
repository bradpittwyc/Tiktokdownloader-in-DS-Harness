"""编排层（PipelineOrchestrator）的回归网。

这一层要证明的是「阶段按状态可靠执行」，而不是「AI 好不好用」：

- 三个阶段（download / transcript / enrich）按链路顺序自动接续
- 任务状态 -> 内容状态的映射尊重既有数据模型
  （download / transcript 只有 pending -> running -> done|failed，AI 才有 queued）
- 成功 / 失败 / 重试 / 达到上限后停止，都真实落到内容表上
- 程序重启：待执行任务继续；中断在 running 的任务被收回来，内容状态不会卡在「分析中」
- 重复提交同一条内容不会排两个任务
- 事件、状态查询、取消、批量提交的行为稳定

全部离线：模型调用注入假 HTTP，视频/字幕用临时文件。
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_factory.ai_enrichment import EnrichmentService  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.jobs import STATE_FAILED, STATE_QUEUED  # noqa: E402
from content_factory.orchestrator import (  # noqa: E402
    PipelineOrchestrator, build_orchestrator, is_permanent_reason)
from content_factory.pipeline import ContentPipeline  # noqa: E402
from content_factory.queue import (  # noqa: E402
    EVENT_FAILED, EVENT_QUEUED, EVENT_RETRYING, EVENT_STARTED, EVENT_SUCCEEDED)
from content_factory.retry import NonRetryableError, RetryPolicy  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402

GOOD_REPLY = json.dumps({
    "topic": "AI 科技", "subtopic": "AI 与就业", "cefr_level": "B2", "accent": "美音",
    "speech_speed": "偏快", "learning_value": 0.92, "keywords": ["collapse"],
    "expressions": [{"text": "the wrong question", "meaning_zh": "错的问题"}],
    "grammar_points": ["宾语从句"],
    "key_sentences": [{"text": "That is the wrong question.", "translation_zh": "那是个错问题。"}],
    "summary_zh": "思考成本在下降。", "recommended_task": "复述",
}, ensure_ascii=False)

# 退避压到 0：测重试逻辑，但不测「能不能等」
FAST = {
    "download": RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0),
    "transcript": RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0),
    "enrich": RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0),
}


class FakeResponse:
    def __init__(self, text):
        self._text = text

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self._text}}]}


class FakeHttp:
    """按脚本返回回复；元素是 Exception 时就抛，用来制造失败。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(url)
        reply = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        if isinstance(reply, Exception):
            raise reply
        return FakeResponse(reply)


class FakeDownloader:
    """假的下载适配器：只证明「阶段被按顺序调用了」。"""

    def __init__(self, result=None, fail_times=0):
        self.result = result or {}
        self.fail_times = int(fail_times or 0)
        self.calls = []

    def download_job(self, job, item):
        self.calls.append(job.content_id)
        if len(self.calls) <= self.fail_times:
            return {"ok": False, "error": "网络请求超时"}
        return {"ok": True, **self.result}


class OrchestratorCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.store = FactoryStore(self.root / "content-factory.db")
        self.settings = FactorySettings(self.root)
        self.events = []
        self.pipeline = ContentPipeline(self.store, self.settings, emit=self.record)
        self._orchestrators = []

    def tearDown(self):
        for orchestrator in self._orchestrators:
            try:
                orchestrator.close()
            except Exception:
                pass
        self.env.stop()
        self.temp.cleanup()

    # ---- 小工具 --------------------------------------------------------
    def record(self, name, payload):
        self.events.append((name, payload))

    def make(self, downloader=None, concurrency=1, policies=None, **kwargs):
        orchestrator = PipelineOrchestrator(
            store=self.store, settings=self.settings, pipeline=self.pipeline,
            downloader=downloader, emit=self.record, concurrency=concurrency,
            policies=policies or FAST, **kwargs)
        self._orchestrators.append(orchestrator)
        return orchestrator

    def enable_ai(self, replies=None):
        self.settings.update("ai", {"api_key": "sk-test", "model": "deepseek-chat"})
        self.pipeline._enricher = EnrichmentService(
            self.settings, http=FakeHttp(replies or [GOOD_REPLY]))

    def local_item(self, index=0, transcript=True):
        """本地视频 + 字幕入库：download 已完成，transcript / ai 待做。

        文件名必须每条不同：create_from_local 用文件名当 source_video_id，
        同名会被去重成同一条内容（既有行为，这里顺着它走）。
        """
        folder = self.root / f"videos{index}"
        folder.mkdir(exist_ok=True)
        (folder / f"clip{index}.mp4").write_bytes(b"x")
        (folder / f"clip{index}.srt").write_text(
            f"1\n00:00:01,000 --> 00:00:02,000\nline {index} about AI\n", encoding="utf-8")
        item_id = self.pipeline.create_from_local(folder)["ids"][0]
        if not transcript:
            self.store.update_item(item_id, transcript_status="pending")
        return item_id

    def remote_item(self, index=0):
        self.pipeline.ingest_videos([{"id": f"v{index}", "title": f"T{index}",
                                      "url": f"https://www.tiktok.com/@x/video/{index}"}])
        return self.store.find_item_by_source("tiktok", f"v{index}")["id"]


class StageWiringTests(OrchestratorCase):
    def test_default_stages_are_transcript_and_enrich(self):
        orchestrator = self.make()
        self.assertEqual(orchestrator.registered_stages(), ["enrich", "transcript"])
        self.assertEqual([stage["ready"] for stage in orchestrator.status()["stages"]],
                         [False, True, True], "没有下载适配器时下载阶段是未接入状态")

    def test_registering_a_downloader_enables_the_download_stage(self):
        orchestrator = self.make()
        orchestrator.set_downloader(FakeDownloader())
        self.assertIn("download", orchestrator.registered_stages())

    def test_unknown_stage_is_rejected_at_submit_time(self):
        orchestrator = self.make()
        item_id = self.local_item()
        result = orchestrator.submit(item_id, "publish")
        self.assertFalse(result["ok"])
        self.assertIn("阶段未接入", result["error"])
        self.assertEqual(orchestrator.counts()["total"], 0, "不认识的阶段不该进队列")

    def test_submitting_for_a_missing_content_is_reported(self):
        orchestrator = self.make()
        result = orchestrator.submit_item("item_missing")
        self.assertFalse(result["ok"])
        self.assertIn("内容不存在", result["error"])

    def test_custom_handler_can_replace_a_stage(self):
        orchestrator = self.make()
        calls = []

        def handler(job, item):
            calls.append(job.content_id)
            return {"ok": True, "message": "假阶段"}

        orchestrator.register_handler("enrich", handler)
        item_id = self.remote_item()
        orchestrator.submit(item_id, "enrich")
        orchestrator.run_once()
        self.assertEqual(calls, [item_id])
        self.assertEqual(self.store.item(item_id)["ai_status"], "done")

    def test_a_non_callable_handler_is_refused(self):
        orchestrator = self.make()
        with self.assertRaises(TypeError):
            orchestrator.register_handler("enrich", "not callable")


class SubmitAndChainTests(OrchestratorCase):
    def test_submit_item_queues_only_the_first_missing_stage(self):
        orchestrator = self.make()
        item_id = self.local_item()
        result = orchestrator.submit_item(item_id)
        self.assertTrue(result["created"])
        self.assertEqual(result["job"]["kind"], "transcript")
        self.assertEqual(result["job"]["payload"]["chain"], ["enrich"])
        self.assertEqual(result["stages"], ["transcript", "enrich"])

    def test_transcript_status_has_no_queued_state(self):
        """download / transcript 的既有状态机只有 pending，不能凭空造出 queued。"""
        orchestrator = self.make()
        item_id = self.local_item()
        orchestrator.submit_item(item_id, stages=("transcript",))
        self.assertEqual(self.store.item(item_id)["transcript_status"], "pending")
        self.assertEqual(self.store.item(item_id)["ai_status"], "pending")

    def test_enrich_status_uses_queued(self):
        """AI 阶段有 queued（批量入队后界面要显示「排队中」）。"""
        orchestrator = self.make(concurrency=1)
        self.enable_ai()
        item_id = self.local_item()
        self.pipeline.set_transcript(item_id, "hello world transcript")
        orchestrator.submit_item(item_id, stages=("enrich",))
        self.assertEqual(self.store.item(item_id)["ai_status"], "queued")

    def test_duplicate_submit_creates_exactly_one_job(self):
        orchestrator = self.make()
        item_id = self.local_item()
        first = orchestrator.submit_item(item_id)
        second = orchestrator.submit_item(item_id)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(len(orchestrator.queue.jobs(content_id=item_id)), 1)

    def test_nothing_to_do_is_reported_not_queued(self):
        orchestrator = self.make()
        self.enable_ai()
        item_id = self.local_item()
        self.store.update_item(item_id, transcript_status="done", ai_status="done")
        result = orchestrator.submit_item(item_id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 0)
        self.assertIn("没有待执行的阶段", result["message"])

    def test_restart_flag_requeues_a_finished_stage(self):
        orchestrator = self.make()
        self.enable_ai()
        item_id = self.local_item()
        self.store.update_item(item_id, transcript_status="done", ai_status="done")
        result = orchestrator.submit_item(item_id, stages=("enrich",), restart=True)
        self.assertTrue(result["created"])
        self.assertEqual(self.store.item(item_id)["ai_status"], "queued")

    def test_submit_many_keeps_going_after_a_bad_id(self):
        orchestrator = self.make()
        good_one = self.local_item(0)
        good_two = self.local_item(1)
        result = orchestrator.submit_many([good_one, "item_missing", good_two])
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(orchestrator.counts()["queued"], 2)

    def test_resubmit_failed_only_picks_up_failed_content(self):
        orchestrator = self.make()
        failed = self.local_item(0)
        healthy = self.local_item(1)
        self.store.update_item(failed, ai_status="failed", last_error="网关 502")
        self.store.update_item(healthy, ai_status="done")
        result = orchestrator.resubmit_failed(stages=("enrich",))
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["jobs"][0]["contentId"], failed)
        self.assertEqual(self.store.item(failed)["ai_status"], "queued")

    def test_resubmit_failed_says_so_when_there_is_nothing_to_do(self):
        orchestrator = self.make()
        self.local_item(0)
        result = orchestrator.resubmit_failed(stages=("enrich",))
        self.assertTrue(result["ok"])
        self.assertEqual(result["created"], 0)
        self.assertIn("没有失败的内容", result["message"])


class StageExecutionTests(OrchestratorCase):
    def test_transcript_stage_succeeds_and_chains_to_enrich(self):
        orchestrator = self.make()
        self.enable_ai()
        item_id = self.local_item()
        orchestrator.submit_item(item_id)

        orchestrator.run_once()                       # transcript
        item = self.store.item(item_id)
        self.assertEqual(item["transcript_status"], "done")
        self.assertIn("line 0", item["transcript_text"])
        self.assertEqual(item["ai_status"], "queued", "转写完成后自动接上 AI 标注")
        kinds = [job.kind for job in orchestrator.queue.jobs(content_id=item_id)]
        self.assertEqual(kinds, ["transcript", "enrich"])

        orchestrator.run_once()                       # enrich
        item = self.store.item(item_id)
        self.assertEqual(item["ai_status"], "done")
        self.assertEqual(item["last_error"], "")
        self.assertEqual(self.store.enrichment(item_id)["topic"], "AI 科技")

    def test_full_chain_download_transcript_enrich(self):
        downloader = FakeDownloader()
        orchestrator = self.make(downloader=downloader)
        self.enable_ai()
        item_id = self.remote_item()
        result = orchestrator.submit_item(item_id)
        self.assertEqual(result["job"]["kind"], "download")
        self.assertEqual(result["job"]["payload"]["chain"], ["transcript", "enrich"])
        self.assertEqual(self.store.item(item_id)["download_status"], "pending")

        orchestrator.run_once()                       # download
        self.assertEqual(downloader.calls, [item_id])
        self.assertEqual(self.store.item(item_id)["download_status"], "done")

        orchestrator.run_once()                       # transcript（没有媒体文件，必然失败）
        item = self.store.item(item_id)
        self.assertEqual(item["transcript_status"], "failed")
        self.assertTrue(item["last_error"], "失败必须写明原因")
        self.assertEqual(item["ai_status"], "pending",
                         "上一步没成功就不该继续往下跑")
        kinds = [job.kind for job in orchestrator.queue.jobs(content_id=item_id)]
        self.assertEqual(kinds, ["download", "transcript"], "链路在失败处停住，不会跳过失败阶段")

    def test_full_chain_succeeds_when_the_downloader_provides_media(self):
        media = self.root / "chain.mp4"
        media.write_bytes(b"x")
        subtitle = self.root / "chain.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nAI is changing work.\n",
                            encoding="utf-8")
        downloader = FakeDownloader({"localVideoPath": str(media),
                                     "localSubtitlePath": str(subtitle)})
        orchestrator = self.make(downloader=downloader)
        self.enable_ai()
        item_id = self.remote_item()
        orchestrator.submit_item(item_id)
        orchestrator.start(recover=False)
        try:
            self.assertTrue(orchestrator.run_until_idle(timeout=90))
        finally:
            orchestrator.stop()
        item = self.store.item(item_id)
        self.assertEqual((item["download_status"], item["transcript_status"], item["ai_status"]),
                         ("done", "done", "done"))
        self.assertEqual(self.store.enrichment(item_id)["topic"], "AI 科技")

    def test_download_result_fields_are_written_back_to_the_content(self):
        media = self.root / "downloaded.mp4"
        media.write_bytes(b"x")
        subtitle = self.root / "downloaded.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8")
        downloader = FakeDownloader({"localVideoPath": str(media),
                                     "localSubtitlePath": str(subtitle),
                                     "duration": 42})
        orchestrator = self.make(downloader=downloader)
        item_id = self.remote_item()
        orchestrator.submit_item(item_id, stages=("download",))
        orchestrator.run_once()
        item = self.store.item(item_id)
        self.assertEqual(item["local_video_path"], str(media))
        self.assertEqual(item["local_subtitle_path"], str(subtitle))
        self.assertEqual(item["duration"], 42)

    def test_worker_pool_runs_the_whole_queue(self):
        orchestrator = self.make(concurrency=2)
        self.enable_ai()
        ids = [self.local_item(index) for index in range(3)]
        for item_id in ids:
            self.store.update_item(item_id, transcript_status="done")
            self.pipeline.set_transcript(item_id, "some transcript text")
        orchestrator.submit_many(ids, stages=("enrich",))
        orchestrator.start(recover=False)
        try:
            self.assertTrue(orchestrator.run_until_idle(timeout=60), "队列应该在超时前跑完")
        finally:
            orchestrator.stop()
        for item_id in ids:
            self.assertEqual(self.store.item(item_id)["ai_status"], "done")
        self.assertLessEqual(orchestrator.pool.stats()["maxActive"], 2)

    def test_run_until_idle_returns_when_there_is_nothing_to_do(self):
        orchestrator = self.make()
        self.assertTrue(orchestrator.run_until_idle(timeout=1))


class FailureAndRetryTests(OrchestratorCase):
    def test_missing_api_key_fails_once_without_burning_retries(self):
        orchestrator = self.make()                    # 故意不配置 API Key
        item_id = self.local_item()
        orchestrator.submit_item(item_id, stages=("enrich",))
        orchestrator.run_once()
        item = self.store.item(item_id)
        self.assertEqual(item["ai_status"], "failed")
        self.assertIn("API Key", item["last_error"])
        job = orchestrator.queue.jobs(content_id=item_id)[0]
        self.assertEqual(job.state, STATE_FAILED)
        self.assertEqual(job.attempts, 1, "永久失败不该反复重试")
        self.assertEqual(orchestrator.counts()["retrying"], 0)

    def test_transient_failure_is_retried_and_then_succeeds(self):
        orchestrator = self.make(policies={"enrich": RetryPolicy(max_attempts=3, base_delay=0.0)})
        calls = []

        def flaky(job, item):
            calls.append(job.attempts)
            if len(calls) == 1:
                return {"ok": False, "error": "模型网关 502"}
            return {"ok": True, "message": "第二次成功"}

        orchestrator.register_handler("enrich", flaky)
        item_id = self.remote_item()
        orchestrator.submit(item_id, "enrich")
        orchestrator.run_once()
        self.assertEqual(self.store.item(item_id)["ai_status"], "queued",
                         "可恢复失败应回到排队态而不是终态失败")
        self.assertIn("502", self.store.item(item_id)["last_error"])
        orchestrator.run_once()
        self.assertEqual(self.store.item(item_id)["ai_status"], "done")
        self.assertEqual(calls, [1, 2])

    def test_retries_stop_at_max_attempts_and_content_stays_failed(self):
        orchestrator = self.make(policies={"enrich": RetryPolicy(max_attempts=3, base_delay=0.0)})
        calls = []

        def always_fails(job, item):
            calls.append(job.attempts)
            return {"ok": False, "error": "上游一直 500"}

        orchestrator.register_handler("enrich", always_fails)
        item_id = self.remote_item()
        orchestrator.submit(item_id, "enrich")
        for _ in range(3):
            orchestrator.run_once()
        self.assertEqual(calls, [1, 2, 3])
        self.assertIsNone(orchestrator.run_once(), "额度用尽后不该再被执行")
        item = self.store.item(item_id)
        self.assertEqual(item["ai_status"], "failed")
        self.assertIn("上游一直 500", item["last_error"])
        self.assertEqual(orchestrator.counts()["failed"], 1)

    def test_handler_exception_is_reported_as_failure(self):
        orchestrator = self.make(policies={"enrich": RetryPolicy(max_attempts=1, base_delay=0.0)})

        def boom(job, item):
            raise RuntimeError("适配器炸了")

        orchestrator.register_handler("enrich", boom)
        item_id = self.remote_item()
        orchestrator.submit(item_id, "enrich")
        orchestrator.run_once()
        item = self.store.item(item_id)
        self.assertEqual(item["ai_status"], "failed")
        self.assertIn("适配器炸了", item["last_error"])

    def test_non_retryable_exception_is_not_retried(self):
        orchestrator = self.make()

        def refuse(job, item):
            raise NonRetryableError("这条内容没有字幕轨，也无法转写")

        orchestrator.register_handler("enrich", refuse)
        item_id = self.remote_item()
        orchestrator.submit(item_id, "enrich")
        orchestrator.run_once()
        job = orchestrator.queue.jobs(content_id=item_id)[0]
        self.assertEqual(job.state, STATE_FAILED)
        self.assertEqual(job.attempts, 1)

    def test_permanent_reason_classifier(self):
        self.assertTrue(is_permanent_reason("未配置 API Key：请在设置里填写"))
        self.assertTrue(is_permanent_reason("内容不存在"))
        self.assertFalse(is_permanent_reason("网络请求超时"))

    def test_a_failure_in_the_queue_does_not_block_other_contents(self):
        orchestrator = self.make(policies={"enrich": RetryPolicy(max_attempts=1, base_delay=0.0)})

        def picky(job, item):
            if item["title"] == "bad":
                return {"ok": False, "error": "坏内容"}
            return {"ok": True}

        orchestrator.register_handler("enrich", picky)
        bad = self.remote_item(1)
        good = self.remote_item(2)
        self.store.update_item(bad, title="bad")
        self.store.update_item(good, title="good")
        orchestrator.submit_many([bad, good], stages=("enrich",))
        orchestrator.start(recover=False)
        try:
            self.assertTrue(orchestrator.run_until_idle(timeout=60))
        finally:
            orchestrator.stop()
        self.assertEqual(self.store.item(bad)["ai_status"], "failed")
        self.assertEqual(self.store.item(good)["ai_status"], "done")


class RestartRecoveryTests(OrchestratorCase):
    def test_interrupted_running_job_is_recovered_and_content_is_not_stuck(self):
        first = self.make()
        item_id = self.remote_item()
        first.submit(item_id, "enrich")
        first.queue.claim(worker_id="worker-1")        # 假装进程在这里被杀掉
        self.assertEqual(self.store.item(item_id)["ai_status"], "running")

        second = self.make()                           # 新进程（新的 run_token）
        summary = second.recover()
        self.assertEqual(summary["recovered"], 1)
        self.assertEqual(self.store.item(item_id)["ai_status"], "queued",
                         "重启后内容不能永远停在「分析中」")
        job = second.queue.jobs(content_id=item_id)[0]
        self.assertEqual(job.state, STATE_QUEUED)
        self.assertEqual(job.attempts, 1)

    def test_recovery_gives_up_and_marks_the_content_failed(self):
        first = self.make()
        item_id = self.remote_item()
        first.submit(item_id, "enrich", max_attempts=1)
        first.queue.claim()
        second = self.make()
        summary = second.recover()
        self.assertEqual(summary["abandoned"], 1)
        item = self.store.item(item_id)
        self.assertEqual(item["ai_status"], "failed")
        self.assertIn("异常退出", item["last_error"])

    def test_pending_jobs_are_executed_after_a_restart(self):
        self.enable_ai()
        first = self.make()
        ids = [self.local_item(1), self.local_item(2)]
        for item_id in ids:
            self.pipeline.set_transcript(item_id, "hello world transcript for restart")
            first.submit(item_id, "enrich")
        self.assertEqual(first.counts()["queued"], 2)
        first.stop()

        second = self.make()                           # 新进程：只剩数据库里的任务
        second.start()
        try:
            self.assertTrue(second.run_until_idle(timeout=90))
        finally:
            second.stop()
        for item_id in ids:
            self.assertEqual(self.store.item(item_id)["ai_status"], "done")

    def test_start_runs_recovery_before_workers(self):
        first = self.make()
        item_id = self.remote_item()
        first.submit(item_id, "enrich")
        first.queue.claim()                            # 上次进程留下的孤儿任务
        second = self.make()
        summary = second.start()
        try:
            self.assertEqual(summary["recovered"], 1)
            self.assertTrue(second.pool.started)
            self.assertTrue(second.run_until_idle(timeout=30), "worker 应该接手恢复后的任务")
        finally:
            second.stop()
        counts = second.counts()
        self.assertEqual(counts["queued"] + counts["running"] + counts["retrying"], 0,
                         "恢复之后队列要能自己收敛到终态")
        self.assertNotEqual(self.store.item(item_id)["ai_status"], "running",
                            "重启后不能有内容永远停在「分析中」")


class EventAndStatusTests(OrchestratorCase):
    def test_state_change_events_are_emitted(self):
        orchestrator = self.make()
        seen = []
        orchestrator.events.subscribe(lambda name, payload: seen.append(name))
        item_id = self.local_item()
        orchestrator.submit_item(item_id, stages=("transcript",))
        orchestrator.run_once()
        self.assertEqual(seen[0], EVENT_QUEUED)
        self.assertIn(EVENT_STARTED, seen)
        self.assertIn(EVENT_SUCCEEDED, seen)

    def test_retry_and_failure_events(self):
        orchestrator = self.make(policies={"enrich": RetryPolicy(max_attempts=2, base_delay=0.0)})
        seen = []
        orchestrator.events.subscribe(lambda name, payload: seen.append(name))
        orchestrator.register_handler("enrich", lambda job, item: {"ok": False, "error": "网络超时"})
        item_id = self.remote_item()
        orchestrator.submit(item_id, "enrich")
        orchestrator.run_once()
        orchestrator.run_once()
        self.assertIn(EVENT_RETRYING, seen)
        self.assertIn(EVENT_FAILED, seen)

    def test_events_reach_the_external_sink(self):
        orchestrator = self.make()
        item_id = self.local_item()
        orchestrator.submit_item(item_id, stages=("transcript",))
        names = [name for name, _payload in self.events]
        self.assertIn(EVENT_QUEUED, names)

    def test_status_reports_queue_workers_and_stages(self):
        orchestrator = self.make(concurrency=2)
        item_id = self.local_item()
        orchestrator.submit_item(item_id, stages=("transcript",))
        status = orchestrator.status()
        self.assertTrue(status["ok"])
        self.assertEqual(status["queue"]["counts"]["queued"], 1)
        self.assertEqual(status["workers"]["concurrency"], 2)
        self.assertEqual([stage["kind"] for stage in status["stages"]],
                         ["download", "transcript", "enrich"])
        self.assertEqual(status["stages"][2]["field"], "ai_status")

    def test_content_jobs_lists_the_history_of_one_item(self):
        orchestrator = self.make()
        item_id = self.local_item()
        orchestrator.submit_item(item_id, stages=("transcript",))
        rows = orchestrator.content_jobs(item_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contentId"], item_id)
        self.assertTrue(rows[0]["active"])

    def test_cancelling_a_queued_job_returns_the_content_to_pending(self):
        orchestrator = self.make()
        self.enable_ai()
        item_id = self.local_item()
        self.pipeline.set_transcript(item_id, "text")
        job = orchestrator.submit_item(item_id, stages=("enrich",))["job"]
        self.assertEqual(self.store.item(item_id)["ai_status"], "queued")
        orchestrator.queue.cancel(job["id"], reason="用户取消")
        self.assertEqual(self.store.item(item_id)["ai_status"], "pending",
                         "取消之后内容应该回到可重试的状态")
        self.assertEqual(orchestrator.counts()["cancelled"], 1)

    def test_legacy_events_are_opt_in(self):
        plain = self.make()
        item_id = self.remote_item()
        plain.register_handler("enrich", lambda job, item: {"ok": True})
        plain.submit(item_id, "enrich")
        plain.run_once()
        legacy = [payload for name, payload in self.events if payload.get("legacy")]
        self.assertEqual(legacy, [], "默认不发兼容事件，避免与既有 pipeline 重复")

        self.events.clear()
        talkative = self.make(legacy_events=True)
        other = self.remote_item(9)
        talkative.register_handler("enrich", lambda job, item: {"ok": True})
        talkative.submit(other, "enrich")
        talkative.run_once()
        legacy = [payload for name, payload in self.events if payload.get("legacy")]
        self.assertTrue(legacy)
        self.assertEqual(legacy[0]["id"], other, "兼容事件的 id 必须是内容 id")


class BuilderTests(OrchestratorCase):
    def test_build_orchestrator_wires_settings_and_concurrency(self):
        self.settings.update("work_mode", {"concurrent_tasks": 7})
        orchestrator = build_orchestrator(store=self.store, settings=self.settings,
                                          pipeline=self.pipeline, emit=self.record)
        self._orchestrators.append(orchestrator)
        self.assertEqual(orchestrator.pool.concurrency, 7)
        self.assertEqual(orchestrator.registered_stages(), ["enrich", "transcript"])

    def test_build_orchestrator_can_autostart_and_stop(self):
        orchestrator = build_orchestrator(store=self.store, settings=self.settings,
                                          pipeline=self.pipeline, emit=self.record,
                                          autostart=True)
        self._orchestrators.append(orchestrator)
        try:
            self.assertTrue(orchestrator.pool.started)
            self.assertEqual(orchestrator.recover()["recovered"], 0)
        finally:
            orchestrator.stop()
        self.assertFalse(orchestrator.pool.started)


if __name__ == "__main__":
    unittest.main()
