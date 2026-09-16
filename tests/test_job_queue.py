"""任务编排内核（Job / JobQueue / RetryPolicy / WorkerPool）的回归网。

全部离线、全部走真实 sqlite（临时目录），不碰网络、不起界面。
锁住的行为都是「编排可靠」的硬要求：

- FIFO 与优先级：先来先跑，高优先级插队
- 任务成功 / 失败
- 失败重试（带退避），达到 max_attempts 后停在 failed
- 程序重启：queued 任务还在；running 的孤儿任务被收回重排（或按次数放弃）
- 重复任务防护：同一条内容的同一阶段只允许一条活跃任务
- 内容级互斥：同一条内容不会同时跑两个阶段
- 并发上限：worker 数 = 并发上限，实测并发不大于它
- 事件：状态变化有事件，顺序可预期
"""

import os
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_factory.jobs import (  # noqa: E402
    Job, JobStore, STATE_CANCELLED, STATE_DONE, STATE_FAILED, STATE_PENDING,
    STATE_QUEUED, STATE_RETRYING, STATE_RUNNING)
from content_factory.queue import (  # noqa: E402
    EVENT_FAILED, EVENT_QUEUED, EVENT_RECOVERED, EVENT_RETRYING, EVENT_STARTED,
    EVENT_SUCCEEDED, JobEvents, JobQueue, WorkerPool)
from content_factory.retry import (  # noqa: E402
    NonRetryableError, RetryPolicy, policies_from_settings)

# 测试里把退避压到 0：重试逻辑要测，但没人愿意等真实的指数退避
FAST = {
    "download": RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0),
    "transcript": RetryPolicy(max_attempts=2, base_delay=0.0, max_delay=0.0),
    "enrich": RetryPolicy(max_attempts=3, base_delay=0.0, max_delay=0.0),
}


class TempQueueCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.db = self.root / "content-factory.db"
        self._stores = []

    def tearDown(self):
        # 任务表用共享长连接（否则每次写入都要重开连接，慢盘上要 1 秒以上），
        # 所以测试结束必须显式关掉，否则 Windows 上临时目录删不掉
        for store in self._stores:
            try:
                store.close()
            except Exception:
                pass
        self.env.stop()
        self.temp.cleanup()

    def make_queue(self, run_token="run-A", policies=None, **kwargs):
        queue = JobQueue(path=self.db, run_token=run_token, policies=policies or FAST, **kwargs)
        self._stores.append(queue.store)
        return queue

    def make_store(self):
        store = JobStore(self.db)
        self._stores.append(store)
        return store


class RetryPolicyTests(unittest.TestCase):
    def test_delays_grow_then_cap(self):
        policy = RetryPolicy(max_attempts=6, base_delay=1.0, factor=2.0, max_delay=5.0)
        self.assertEqual(policy.delay_for(1), 1.0)
        self.assertEqual(policy.delay_for(2), 2.0)
        self.assertEqual(policy.delay_for(3), 4.0)
        self.assertEqual(policy.delay_for(4), 5.0, "退避不能超过 max_delay")

    def test_max_attempts_is_total_attempts_not_extra_retries(self):
        policy = RetryPolicy(max_attempts=3)
        self.assertTrue(policy.allows(1))
        self.assertTrue(policy.allows(2))
        self.assertFalse(policy.allows(3), "第 3 次尝试之后不再重试")

    def test_one_attempt_means_no_retry(self):
        policy = RetryPolicy(max_attempts=1)
        self.assertFalse(policy.decide(1).retry)

    def test_zero_and_garbage_parameters_are_clamped(self):
        policy = RetryPolicy(max_attempts=0, base_delay=-5, factor=0.1, max_delay=-1, jitter=9)
        self.assertEqual(policy.max_attempts, 1)
        self.assertEqual(policy.base_delay, 0.0)
        self.assertEqual(policy.factor, 1.0)
        self.assertEqual(policy.max_delay, 0.0)
        self.assertEqual(policy.jitter, 1.0)

    def test_non_retryable_error_stops_immediately(self):
        policy = RetryPolicy(max_attempts=5, base_delay=0.0)
        decision = policy.decide(1, error=NonRetryableError("没配 Key"))
        self.assertFalse(decision.retry)
        self.assertIn("NonRetryableError", decision.reason)

    def test_retry_on_whitelist(self):
        policy = RetryPolicy(max_attempts=5, retry_on=(ValueError,))
        self.assertTrue(policy.decide(1, error=ValueError("x")).retry)
        self.assertFalse(policy.decide(1, error=KeyError("x")).retry)

    def test_no_retry_on_blacklist_wins(self):
        policy = RetryPolicy(max_attempts=5, retry_on=(Exception,), no_retry_on=(KeyError,))
        self.assertFalse(policy.decide(1, error=KeyError("x")).retry)

    def test_jitter_stays_within_bounds(self):
        policy = RetryPolicy(max_attempts=3, base_delay=10.0, jitter=0.5)
        self.assertEqual(policy.delay_for(1, rand=lambda: 0.5), 10.0, "抖动中点等于原值")
        self.assertEqual(policy.delay_for(1, rand=lambda: 0.0), 5.0)
        self.assertEqual(policy.delay_for(1, rand=lambda: 1.0), 15.0)

    def test_settings_section_is_optional_and_per_kind(self):
        class Settings:
            def __init__(self, section):
                self._section = section

            def section(self, name):
                return self._section.get(name, {})

        from content_factory.retry import ENRICH_RETRY
        default = policies_from_settings(Settings({}))
        self.assertEqual(default["enrich"], ENRICH_RETRY, "没有 jobs 分区时用默认策略")
        tuned = policies_from_settings(Settings({"jobs": {"enrich": {"max_attempts": 7},
                                                          "base_delay": 3}}))
        self.assertEqual(tuned["enrich"].max_attempts, 7)
        self.assertEqual(tuned["enrich"].base_delay, 3)
        self.assertEqual(tuned["download"].max_attempts, 3)


class JobModelTests(unittest.TestCase):
    def test_defaults_and_labels(self):
        job = Job(id="job_1", kind="enrich", content_id="item_1")
        self.assertEqual(job.dedupe_key, "enrich:item_1")
        self.assertTrue(job.is_active)
        self.assertEqual(job.state_label, "待处理")
        self.assertEqual(job.retries_left, 3)

    def test_round_trip_through_a_row(self):
        row = {"id": "job_1", "seq": 4, "kind": "enrich", "content_id": "item_1",
               "dedupe_key": "enrich:item_1", "state": STATE_RUNNING, "priority": 3,
               "attempts": 2, "max_attempts": 5, "payload": '{"chain": ["enrich"]}',
               "result": '{"topic": "AI"}', "error": "boom", "available_at": 12.5,
               "lease_owner": "w1", "lease_expires_at": 99.0, "run_token": "run-A",
               "created_at": "2026-01-01 00:00:00", "updated_at": "2026-01-01 00:00:01",
               "started_at": "2026-01-01 00:00:01", "finished_at": ""}
        job = Job.from_row(row)
        self.assertEqual(job.id, "job_1")
        self.assertEqual(job.payload, {"chain": ["enrich"]})
        self.assertEqual(job.result, {"topic": "AI"})
        self.assertEqual(job.state, STATE_RUNNING)
        self.assertEqual(job.priority, 3)
        self.assertEqual(job.attempts, 2)
        self.assertEqual(job.max_attempts, 5)
        self.assertEqual(job.retries_left, 3)
        self.assertEqual(job.lease_owner, "w1")
        self.assertEqual(job.to_dict()["contentId"], "item_1")

    def test_broken_json_payload_does_not_crash(self):
        job = Job.from_row({"id": "j", "kind": "enrich", "payload": "{not json",
                            "result": None})
        self.assertEqual(job.payload, {})
        self.assertEqual(job.result, {})

    def test_copy_does_not_mutate_the_original(self):
        job = Job(id="j1", kind="enrich", state=STATE_QUEUED)
        other = job.copy(state=STATE_DONE)
        self.assertEqual(job.state, STATE_QUEUED)
        self.assertEqual(other.state, STATE_DONE)


class JobStoreTests(TempQueueCase):
    def test_tables_are_created_next_to_the_content_tables(self):
        store = self.make_queue().store
        store.init()
        with closing(store.connect()) as connection:
            names = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            indexes = {row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertIn("jobs", names)
        self.assertIn("idx_jobs_active_dedupe", indexes)
        self.assertEqual(mode.lower(), "wal")

    def test_active_dedupe_is_enforced_by_the_database(self):
        """就算绕过队列的检查直接插两次，数据库也不允许两条活跃任务。"""
        store = self.make_store()
        store.init()
        store.insert(Job(id="a", kind="enrich", content_id="item_1", dedupe_key="enrich:item_1"))
        with self.assertRaises(sqlite3.IntegrityError):
            store.insert(Job(id="b", kind="enrich", content_id="item_1",
                             dedupe_key="enrich:item_1"))
        # 终态之后可以再排一条（重跑）
        store.update("a", state=STATE_DONE)
        store.insert(Job(id="c", kind="enrich", content_id="item_1", dedupe_key="enrich:item_1"))
        self.assertEqual(store.counts()["total"], 2)

    def test_seq_is_assigned_by_the_store_for_fifo(self):
        store = self.make_store()
        store.init()
        first = store.insert(Job(id="a", kind="enrich", content_id="i1", dedupe_key="k1"))
        second = store.insert(Job(id="b", kind="enrich", content_id="i2", dedupe_key="k2"))
        self.assertLess(first.seq, second.seq)

    def test_jobs_survive_reopening(self):
        store = self.make_store()
        store.init()
        store.insert(Job(id="a", kind="enrich", content_id="i1", payload={"note": "还在"}))
        reopened = self.make_store()
        job = reopened.get("a")
        self.assertEqual(job.payload["note"], "还在")
        self.assertEqual(job.state, STATE_PENDING)

    def test_claim_skips_a_content_that_is_already_running(self):
        store = self.make_store()
        store.init()
        store.insert(Job(id="a", kind="download", content_id="i1", dedupe_key="download:i1"))
        store.insert(Job(id="b", kind="transcript", content_id="i1", dedupe_key="transcript:i1"))
        first = store.claim(run_token="r1", now=time.time())
        self.assertEqual(first.id, "a")
        self.assertIsNone(store.next_claimable(now=time.time()),
                          "同一条内容已在跑，不能再认领它的另一个阶段")

    def test_stale_running_detects_other_run_and_expired_lease(self):
        store = self.make_store()
        store.init()
        store.insert(Job(id="a", kind="enrich", content_id="i1", dedupe_key="k1"))
        store.claim(run_token="run-A", now=1000.0, lease_seconds=10.0)
        self.assertEqual([job.id for job in store.stale_running(now=1005.0, run_token="run-A")], [],
                         "本进程租约未到期，不算孤儿")
        self.assertEqual([job.id for job in store.stale_running(now=1005.0, run_token="run-B")],
                         ["a"], "换了进程运行标识就是上次遗留的")
        self.assertEqual([job.id for job in store.stale_running(now=2000.0, run_token="run-A")],
                         ["a"], "租约过期同样是孤儿")


class JobQueueBasicsTests(TempQueueCase):
    def test_enqueue_then_claim_then_finish(self):
        queue = self.make_queue()
        result = queue.enqueue("enrich", "item_1")
        self.assertTrue(result["created"])
        job = result["job"]
        self.assertEqual(job["state"], STATE_QUEUED)
        self.assertEqual(job["dedupeKey"], "enrich:item_1")

        claimed = queue.claim(worker_id="w1")
        self.assertEqual(claimed.id, job["id"])
        self.assertEqual(claimed.state, STATE_RUNNING)
        self.assertEqual(claimed.attempts, 1, "认领即记一次尝试（崩溃也算）")
        self.assertEqual(claimed.lease_owner, "w1")

        done = queue.finish(claimed, result={"topic": "AI 科技"})
        self.assertEqual(done.state, STATE_DONE)
        self.assertEqual(done.result["topic"], "AI 科技")
        self.assertTrue(queue.is_idle())
        self.assertEqual(queue.counts()["done"], 1)

    def test_fifo_order(self):
        queue = self.make_queue()
        for index in range(4):
            queue.enqueue("enrich", f"item_{index}")
        claimed = [queue.claim().content_id for _ in range(4)]
        self.assertEqual(claimed, ["item_0", "item_1", "item_2", "item_3"])

    def test_priority_beats_fifo_but_fifo_holds_within_a_priority(self):
        queue = self.make_queue()
        queue.enqueue("enrich", "low_1", priority=0)
        queue.enqueue("enrich", "low_2", priority=0)
        queue.enqueue("enrich", "high_1", priority=5)
        queue.enqueue("enrich", "high_2", priority=5)
        claimed = [queue.claim().content_id for _ in range(4)]
        self.assertEqual(claimed, ["high_1", "high_2", "low_1", "low_2"])

    def test_delayed_job_is_not_claimable_before_its_time(self):
        queue = self.make_queue()
        queue.enqueue("enrich", "later", delay=60)
        self.assertEqual(queue.get(queue.jobs()[0].id).state, STATE_PENDING)
        self.assertIsNone(queue.claim())
        self.assertIsNotNone(queue.store.next_claimable(now=time.time() + 61))

    def test_duplicate_active_job_is_not_enqueued_twice(self):
        queue = self.make_queue()
        first = queue.enqueue("enrich", "item_1")
        again = queue.enqueue("enrich", "item_1")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["job"]["id"], first["job"]["id"])
        self.assertEqual(queue.counts()["total"], 1)

    def test_different_stages_of_one_content_are_separate_jobs(self):
        queue = self.make_queue()
        queue.enqueue("download", "item_1")
        queue.enqueue("transcript", "item_1")
        self.assertEqual(queue.counts()["total"], 2)
        self.assertEqual(len(queue.store.active_content_ids()), 1)

    def test_a_finished_stage_can_be_queued_again(self):
        queue = self.make_queue()
        job = queue.enqueue("enrich", "item_1")["job"]
        queue.finish(queue.claim())
        again = queue.enqueue("enrich", "item_1")
        self.assertTrue(again["created"], "终态之后允许重跑")
        self.assertNotEqual(again["job"]["id"], job["id"])

    def test_enqueue_many_keeps_going_after_a_bad_entry(self):
        queue = self.make_queue()
        result = queue.enqueue_many([
            {"kind": "enrich", "contentId": "a"},
            {"kind": "", "contentId": "b"},
            "不是字典",
            {"kind": "enrich", "contentId": "c"},
            {"kind": "enrich", "contentId": "a"},          # 重复
        ])
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["rejected"], 2)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(queue.counts()["queued"], 2)

    def test_cancel_removes_the_job_from_the_queue(self):
        queue = self.make_queue()
        job = queue.enqueue("enrich", "item_1")["job"]
        cancelled = queue.cancel(job["id"], reason="用户取消")
        self.assertEqual(cancelled.state, STATE_CANCELLED)
        self.assertIsNone(queue.claim())
        # 取消之后可以重新排队（活跃去重不再命中）
        self.assertTrue(queue.enqueue("enrich", "item_1")["created"])

    def test_finish_after_cancel_does_not_resurrect_the_job(self):
        queue = self.make_queue()
        job = queue.enqueue("enrich", "item_1")["job"]
        claimed = queue.claim()
        queue.cancel(claimed)
        queue.finish(claimed, result={"ok": True})
        self.assertEqual(queue.get(job["id"]).state, STATE_CANCELLED)


class RetryFlowTests(TempQueueCase):
    def test_failure_schedules_a_retry_and_then_succeeds(self):
        queue = self.make_queue()
        queue.enqueue("enrich", "item_1")
        first = queue.claim()
        outcome = queue.fail(first, error="网络超时")
        self.assertTrue(outcome["retry"])
        self.assertEqual(outcome["job"].state, STATE_RETRYING)
        self.assertEqual(queue.get(first.id).attempts, 1)

        second = queue.claim()
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.attempts, 2)
        self.assertEqual(queue.finish(second).state, STATE_DONE)

    def test_backoff_delay_is_persisted_on_the_job(self):
        queue = self.make_queue(policies={"enrich": RetryPolicy(max_attempts=3, base_delay=30.0,
                                                                max_delay=30.0)})
        queue.enqueue("enrich", "item_1")
        first = queue.claim()
        before = queue.now()
        outcome = queue.fail(first, error="稍后再试")
        self.assertAlmostEqual(outcome["delay"], 30.0, places=3)
        fresh = queue.get(first.id)
        self.assertGreaterEqual(fresh.available_at, before + 29.0)
        self.assertIsNone(queue.claim(), "退避时间内不该被再次认领")

    def test_retry_stops_at_max_attempts(self):
        queue = self.make_queue(policies={"enrich": RetryPolicy(max_attempts=3, base_delay=0.0,
                                                                max_delay=0.0)})
        queue.enqueue("enrich", "item_1")
        attempts = []
        for _ in range(3):
            job = queue.claim()
            attempts.append(job.attempts)
            queue.fail(job, error="一直失败")
        self.assertEqual(attempts, [1, 2, 3])
        final = queue.jobs()[0]
        self.assertEqual(final.state, STATE_FAILED)
        self.assertEqual(final.attempts, 3)
        self.assertIsNone(queue.claim(), "达到 max_attempts 后必须停下来")
        self.assertEqual(queue.counts()["failed"], 1)

    def test_permanent_failure_does_not_consume_retries(self):
        queue = self.make_queue()
        queue.enqueue("enrich", "item_1")
        job = queue.claim()
        outcome = queue.fail(job, error="未配置 API Key", permanent=True)
        self.assertFalse(outcome["retry"])
        self.assertEqual(queue.get(job.id).state, STATE_FAILED)
        self.assertEqual(queue.counts()["failed"], 1)
        self.assertEqual(queue.counts()["retrying"], 0)

    def test_job_specific_max_attempts_wins(self):
        queue = self.make_queue()
        queue.enqueue("enrich", "item_1", max_attempts=1)
        job = queue.claim()
        self.assertEqual(job.max_attempts, 1)
        self.assertFalse(queue.fail(job, error="一次就放弃")["retry"])

    def test_one_failure_does_not_stop_other_jobs(self):
        queue = self.make_queue()
        for index in range(3):
            queue.enqueue("enrich", f"item_{index}")
        first = queue.claim()
        queue.fail(first, error="boom", permanent=True)
        rest = [queue.claim().content_id for _ in range(2)]
        self.assertEqual(rest, ["item_1", "item_2"])


class RestartRecoveryTests(TempQueueCase):
    def test_queued_jobs_survive_a_restart(self):
        first = self.make_queue(run_token="run-A")
        first.enqueue("enrich", "item_1")
        first.enqueue("enrich", "item_2")

        # 模拟程序被关掉：进程里什么都不做，直接换一个新队列（新 run_token）
        second = self.make_queue(run_token="run-B")
        pending = second.jobs(state=STATE_QUEUED)
        self.assertEqual([job.content_id for job in pending], ["item_1", "item_2"])
        self.assertEqual(second.claim().content_id, "item_1")
        self.assertEqual(second.counts()["done"], 0)

    def test_delayed_pending_job_survives_a_restart(self):
        first = self.make_queue(run_token="run-A")
        first.enqueue("enrich", "item_1", delay=3600)
        self.assertEqual(first.jobs()[0].state, STATE_PENDING)

        second = self.make_queue(run_token="run-B")
        job = second.jobs()[0]
        self.assertEqual(job.state, STATE_PENDING)
        self.assertEqual(job.content_id, "item_1")
        self.assertEqual(second.recover()["recovered"], 0, "还没到时间的任务不算孤儿")
        self.assertIsNone(second.claim(), "重启后也不能提前跑")
        self.assertIsNotNone(second.store.next_claimable(now=time.time() + 3601),
                             "到点之后自然可以被认领")

    def test_running_job_left_by_a_dead_process_is_requeued(self):
        first = self.make_queue(run_token="run-A")
        first.enqueue("enrich", "item_1")
        crashed = first.claim(worker_id="worker-1")
        self.assertEqual(crashed.state, STATE_RUNNING)

        second = self.make_queue(run_token="run-B")
        summary = second.recover()
        self.assertEqual(summary["recovered"], 1)
        self.assertEqual(summary["abandoned"], 0)
        recovered = second.get(crashed.id)
        self.assertEqual(recovered.state, STATE_QUEUED)
        self.assertEqual(recovered.attempts, 1, "崩溃的那次尝试要算数，否则会无限崩溃重启")
        self.assertIn("异常退出", recovered.error)
        self.assertEqual(recovered.lease_owner, "")

        claimed = second.claim()
        self.assertEqual(claimed.id, crashed.id)
        self.assertEqual(claimed.attempts, 2)

    def test_recovery_gives_up_when_retries_are_used_up(self):
        first = self.make_queue(run_token="run-A")
        first.enqueue("enrich", "item_1", max_attempts=2)
        first.claim()                                   # 第 1 次
        second = self.make_queue(run_token="run-B")
        second.recover()                                # 退回队列（attempts=1）
        second.claim()                                  # 第 2 次（用尽额度）
        third = self.make_queue(run_token="run-C")
        summary = third.recover()
        self.assertEqual(summary["recovered"], 0)
        self.assertEqual(summary["abandoned"], 1)
        self.assertEqual(third.jobs()[0].state, STATE_FAILED)
        self.assertIn("已达最大尝试次数", third.jobs()[0].error)

    def test_expired_lease_is_reaped_within_the_same_run(self):
        queue = self.make_queue(lease_seconds=0.01)
        queue.enqueue("enrich", "item_1")
        queue.claim()
        time.sleep(0.05)
        summary = queue.recover()
        self.assertEqual(summary["recovered"], 1)
        self.assertEqual(queue.jobs()[0].state, STATE_QUEUED)

    def test_recovery_emits_events(self):
        first = self.make_queue(run_token="run-A")
        first.enqueue("enrich", "item_1")
        first.claim()
        second = self.make_queue(run_token="run-B")
        seen = []
        second.events.subscribe(lambda name, payload: seen.append(name))
        second.recover()
        self.assertIn(EVENT_QUEUED, seen)
        self.assertIn(EVENT_RECOVERED, seen)


class ContentLockTests(TempQueueCase):
    def test_same_content_never_runs_two_stages_at_once(self):
        queue = self.make_queue()
        queue.enqueue("download", "item_1")
        queue.enqueue("transcript", "item_1")
        first = queue.claim()
        self.assertEqual(first.kind, "download")
        self.assertIsNone(queue.claim(), "同一条内容的第二个阶段必须等第一个跑完")
        queue.finish(first)
        second = queue.claim()
        self.assertEqual(second.kind, "transcript")

    def test_different_contents_run_independently(self):
        queue = self.make_queue()
        queue.enqueue("download", "item_1")
        queue.enqueue("download", "item_2")
        self.assertEqual(queue.claim().content_id, "item_1")
        self.assertEqual(queue.claim().content_id, "item_2")

    def test_jobs_without_a_content_id_are_deduped_by_kind(self):
        queue = self.make_queue()
        queue.enqueue("enrich", "")
        queue.enqueue("enrich", "")
        self.assertEqual(queue.counts()["queued"], 1, "无内容 id 的同类全局任务也只允许一条")
        queue.claim()
        self.assertIsNone(queue.claim())


class EventTests(TempQueueCase):
    def test_success_event_sequence(self):
        queue = self.make_queue()
        seen = []
        queue.events.subscribe(lambda name, payload: seen.append((name, payload["state"])))
        queue.enqueue("enrich", "item_1")
        queue.finish(queue.claim())
        names = [name for name, _state in seen]
        self.assertEqual(names[:3], [EVENT_QUEUED, EVENT_STARTED, EVENT_SUCCEEDED])
        self.assertEqual(seen[0][1], STATE_QUEUED)
        self.assertEqual(seen[1][1], STATE_RUNNING)
        self.assertEqual(seen[2][1], STATE_DONE)

    def test_failure_then_exhaustion_events(self):
        queue = self.make_queue(policies={"enrich": RetryPolicy(max_attempts=2, base_delay=0.0)})
        seen = []
        queue.events.subscribe(lambda name, payload: seen.append(name))
        queue.enqueue("enrich", "item_1")
        queue.fail(queue.claim(), error="第一次失败")
        queue.fail(queue.claim(), error="第二次失败")
        self.assertIn(EVENT_RETRYING, seen)
        self.assertIn(EVENT_FAILED, seen)

    def test_event_payload_carries_what_the_ui_needs(self):
        queue = self.make_queue()
        queue.enqueue("enrich", "item_1", payload={"chain": ["enrich"]})
        queue.finish(queue.claim(), result={"topic": "AI"})
        payload = dict(queue.events.last(EVENT_SUCCEEDED))
        self.assertEqual(payload["contentId"], "item_1")
        self.assertEqual(payload["id"], "item_1", "沿用既有界面的 id 字段")
        self.assertEqual(payload["stage"], "enrich")
        self.assertEqual(payload["state"], STATE_DONE)
        self.assertEqual(payload["payload"]["chain"], ["enrich"])
        self.assertEqual(payload["result"]["topic"], "AI")
        self.assertIn("queue", payload)

    def test_a_broken_listener_cannot_break_the_queue(self):
        queue = self.make_queue()

        def explode(_name, _payload):
            raise RuntimeError("界面炸了")

        queue.events.subscribe(explode)
        queue.enqueue("enrich", "item_1")
        self.assertEqual(queue.finish(queue.claim()).state, STATE_DONE)

    def test_sink_receives_events_and_history_is_kept(self):
        seen = []
        queue = self.make_queue(emit=lambda name, payload: seen.append(name))
        queue.enqueue("enrich", "item_1")
        self.assertIn(EVENT_QUEUED, seen)
        self.assertEqual(queue.events.history(EVENT_QUEUED)[0]["contentId"], "item_1")
        self.assertIsNotNone(queue.events.last(EVENT_QUEUED))

    def test_events_object_is_usable_on_its_own(self):
        events = JobEvents(limit=10)
        received = []
        listener = events.subscribe(lambda name, payload: received.append(payload))
        events.emit("x", {"a": 1})
        self.assertEqual(received[0]["a"], 1)
        self.assertTrue(events.unsubscribe(listener))
        events.emit("x", {"a": 2})
        self.assertEqual(len(received), 1)


class WorkerPoolTests(TempQueueCase):
    def test_jobs_are_executed_and_marked_done(self):
        queue = self.make_queue()
        pool = WorkerPool(queue, lambda job: {"ok": True, "handled": job.content_id},
                          concurrency=2, poll_interval=0.01)
        for index in range(4):
            queue.enqueue("enrich", f"item_{index}")
        pool.start()
        try:
            self.assertTrue(pool.run_until_idle(timeout=10), "队列应该在超时前跑完")
        finally:
            pool.stop()
        self.assertEqual(queue.counts()["done"], 4)
        self.assertEqual(pool.stats()["processed"], 4)
        self.assertEqual(pool.stats()["failed"], 0)

    def test_concurrency_limit_is_respected(self):
        queue = self.make_queue()
        lock = threading.Lock()
        state = {"active": 0, "max": 0}

        def handler(_job):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            time.sleep(0.05)
            with lock:
                state["active"] -= 1
            return {"ok": True}

        pool = WorkerPool(queue, handler, concurrency=2, poll_interval=0.005)
        for index in range(6):
            queue.enqueue("enrich", f"item_{index}")
        pool.start()
        try:
            self.assertTrue(pool.run_until_idle(timeout=15))
        finally:
            pool.stop()
        self.assertEqual(state["max"], 2, "并发上限就是 worker 数，不能超发")
        self.assertEqual(pool.stats()["maxActive"], 2)
        self.assertEqual(queue.counts()["done"], 6)

    def test_multiple_independent_jobs_all_finish(self):
        queue = self.make_queue()
        handled = []
        pool = WorkerPool(queue, lambda job: handled.append(job.content_id) or {"ok": True},
                          concurrency=4, poll_interval=0.005)
        for index in range(10):
            queue.enqueue("enrich", f"item_{index}")
        pool.start()
        try:
            self.assertTrue(pool.run_until_idle(timeout=15))
        finally:
            pool.stop()
        self.assertEqual(sorted(handled), [f"item_{index}" for index in range(10)])

    def test_handler_exception_is_a_failure_not_a_dead_thread(self):
        queue = self.make_queue(policies={"enrich": RetryPolicy(max_attempts=1, base_delay=0.0)})
        calls = []

        def handler(job):
            calls.append(job.content_id)
            if job.content_id == "bad":
                raise ValueError("炸了")
            return {"ok": True}

        pool = WorkerPool(queue, handler, concurrency=1, poll_interval=0.005)
        queue.enqueue("enrich", "bad")
        queue.enqueue("enrich", "good")
        pool.start()
        try:
            self.assertTrue(pool.run_until_idle(timeout=10))
        finally:
            pool.stop()
        self.assertEqual(calls, ["bad", "good"])
        self.assertEqual(queue.counts()["failed"], 1)
        self.assertEqual(queue.counts()["done"], 1)
        self.assertIn("ValueError", queue.jobs()[0].error)

    def test_non_retryable_exception_is_not_retried(self):
        queue = self.make_queue()
        calls = []

        def handler(job):
            calls.append(job.content_id)
            raise NonRetryableError("永久失败")

        pool = WorkerPool(queue, handler, concurrency=1, poll_interval=0.005)
        queue.enqueue("enrich", "item_1")
        pool.start()
        try:
            self.assertTrue(pool.run_until_idle(timeout=10))
        finally:
            pool.stop()
        self.assertEqual(calls, ["item_1"], "永久失败不应该被反复重试")
        self.assertEqual(queue.counts()["failed"], 1)

    def test_handler_can_report_failure_with_a_dict(self):
        queue = self.make_queue()
        pool = WorkerPool(queue, lambda job: {"ok": False, "error": "没有字幕"},
                          concurrency=1, poll_interval=0.005)
        queue.enqueue("enrich", "item_1")
        pool.start()
        try:
            self.assertTrue(pool.run_until_idle(timeout=10))
        finally:
            pool.stop()
        self.assertIn("没有字幕", queue.jobs()[0].error)

    def test_stop_is_clean_and_restartable(self):
        queue = self.make_queue()
        pool = WorkerPool(queue, lambda job: {"ok": True}, concurrency=1, poll_interval=0.005)
        pool.start()
        self.assertTrue(pool.started)
        pool.stop()
        self.assertFalse(pool.started)
        self.assertFalse(pool.alive)
        queue.enqueue("enrich", "item_1")
        pool.start()
        try:
            self.assertTrue(pool.run_until_idle(timeout=10))
        finally:
            pool.stop()
        self.assertEqual(queue.counts()["done"], 1)

    def test_long_running_job_keeps_its_lease(self):
        """跑得久的任务不该被自己的租约误判成孤儿（否则会被重复执行）。"""
        queue = self.make_queue(lease_seconds=0.3)
        started = threading.Event()

        def handler(_job):
            started.set()
            time.sleep(0.7)
            return {"ok": True}

        pool = WorkerPool(queue, handler, concurrency=1, poll_interval=0.005)
        queue.enqueue("enrich", "item_1")
        pool.start()
        try:
            self.assertTrue(started.wait(5))
            time.sleep(0.4)
            summary = queue.recover()
            self.assertEqual(summary["recovered"], 0, "还在跑的任务不能被回收")
            self.assertTrue(pool.run_until_idle(timeout=10))
        finally:
            pool.stop()
        self.assertEqual(queue.counts()["done"], 1)

    def test_run_once_executes_in_the_calling_thread(self):
        queue = self.make_queue()
        pool = WorkerPool(queue, lambda job: {"ok": True}, concurrency=1)
        self.assertIsNone(pool.run_once())
        queue.enqueue("enrich", "item_1")
        job = pool.run_once()
        self.assertEqual(job.content_id, "item_1")
        self.assertEqual(queue.counts()["done"], 1)


if __name__ == "__main__":
    unittest.main()
