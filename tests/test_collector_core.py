"""自动采集核心的回归网：发现 → 去重 → 判断 → 排队 → 交给下载器。

锁住的行为都是验收项（逐条对应需求）：
- 候选视频发现（含下载器 recognize 的适配、各种脏数据形状）
- 重复视频过滤（作品 id / 链接 / content_key 三层，且链接的查询串不影响判断）
- 已有内容不重复进入任务
- 失败记录（来源失败 → run 失败 + Creator 状态 error + 退避）
- 多次 polling 不重复创建相同 job（这是采集器最容易出的错）
- 采集任务能被**已有下载器**直接消费（形状对得上，下完自动入库）

全部离线：候选项来源用 StaticCandidateSource / 假下载器，不启动浏览器。
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

from content_factory.collector import (ContentCollector, DownloadPolicy,  # noqa: E402
                                       StaticCandidateSource, build_collector,
                                       download_jobs, models)
from content_factory.collector.dedupe import DedupeIndex, key_for, normalize_url  # noqa: E402
from content_factory.collector.runner import CollectorRunner  # noqa: E402
from content_factory.collector.sources import DownloaderCandidateSource  # noqa: E402
from content_factory.collector.store import CollectionJobStore  # noqa: E402
from content_factory.creator_monitor import CreatorMonitorService  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402

VIDEO_A = {"id": "7312000000000000001",
           "url": "https://www.tiktok.com/@emilyintech/video/7312000000000000001",
           "title": "3 habits that changed my morning", "duration": 40, "type": "video"}
VIDEO_B = {"id": "7312000000000000002",
           "url": "https://www.tiktok.com/@emilyintech/video/7312000000000000002",
           "title": "how I plan my week", "duration": 55, "type": "video"}


class TempEnvCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.store = FactoryStore(self.root / "content-factory.db")
        self.settings = FactorySettings(self.root)
        self.settings.update("collect", {"keywords_block": [], "per_creator_limit": 50,
                                         "duration_min_sec": 0, "duration_max_sec": 0,
                                         "fail_retry": 3})
        self.monitor = CreatorMonitorService(self.store, self.settings)
        self.events = []
        self.creator_id = self.monitor.create_creator(
            "emilyintech", display_name="Emily", priority="高", poll_interval="30 分钟")["creatorId"]

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def collector(self, videos=None, **source_kwargs):
        source_kwargs.setdefault("complete", True)
        source = StaticCandidateSource(videos=videos if videos is not None else [VIDEO_A, VIDEO_B],
                                       **source_kwargs)
        return build_collector(self.store, settings=self.settings, source=source,
                               emit=lambda name, payload: self.events.append((name, payload)))


class DedupeTests(unittest.TestCase):
    def test_url_normalization_ignores_noise(self):
        self.assertEqual(normalize_url("https://www.tiktok.com/@a/video/1?is_from_webapp=1#x"),
                         "https://www.tiktok.com/@a/video/1")
        self.assertEqual(normalize_url("https://www.tiktok.com/@a/video/1/"),
                         "https://www.tiktok.com/@a/video/1")
        self.assertEqual(normalize_url("https://M.TikTok.com/@a/video/1"),
                         "https://www.tiktok.com/@a/video/1",
                         "手机端主机名要归一，否则同一条内容算两条")

    def test_content_key_prefers_the_video_id(self):
        self.assertEqual(key_for("tiktok", "7312", "https://x/1"), "tiktok:7312")
        self.assertEqual(key_for("tiktok", "", "https://www.tiktok.com/@a/video/7312"),
                         "tiktok:7312")
        self.assertEqual(key_for("tiktok", "", "https://www.tiktok.com/@a/video/7312?x=1"),
                         "tiktok:7312")
        self.assertEqual(key_for("tiktok", "", "https://www.tiktok.com/@a"),
                         "tiktok:url:https://www.tiktok.com/@a")
        self.assertEqual(key_for("tiktok", "", ""), "")

    def test_index_reports_the_first_duplicate(self):
        index = DedupeIndex()
        self.assertIsNone(index.check("tiktok:1", "https://x/1"))
        self.assertEqual(index.check("tiktok:1", "https://x/1"), "duplicate_key")
        self.assertEqual(index.check("tiktok:2", "https://x/1?y=2"), "duplicate_url")
        self.assertIsNone(index.check("tiktok:3", "https://x/3"))
        self.assertEqual(len(index), 2)


class CandidateModelTests(unittest.TestCase):
    def test_dict_and_tuple_shapes_are_both_understood(self):
        from_dict = models.normalize_candidate(VIDEO_A, handle="emilyintech")
        self.assertEqual(from_dict.source_video_id, "7312000000000000001")
        self.assertEqual(from_dict.content_key, "tiktok:7312000000000000001")
        self.assertEqual(from_dict.kind, "video")

        from_tuple = models.normalize_candidate(
            ("7312000000000000009", "https://www.tiktok.com/@a/video/7312000000000000009",
             "title", "cover", "1.2K", "image"), handle="a")
        self.assertEqual(from_tuple.kind, "photo")
        self.assertEqual(from_tuple.downloader_kind, "image")
        self.assertEqual(from_tuple.title, "title")
        self.assertEqual(from_tuple.cover, "cover")

    def test_junk_is_skipped_not_fatal(self):
        self.assertIsNone(models.normalize_candidate(None))
        self.assertIsNone(models.normalize_candidate({}))
        self.assertIsNone(models.normalize_candidate({"title": "没有 id 也没有链接"}))
        rows = models.normalize_candidates([VIDEO_A, None, "垃圾", {"title": "x"}], handle="a")
        self.assertEqual(len(rows), 1)

    def test_photo_url_without_type_is_detected(self):
        row = models.normalize_candidate({"id": "5", "url": "https://www.tiktok.com/@a/photo/5"})
        self.assertEqual(row.kind, "photo")

    def test_counts_are_parsed_from_profile_text(self):
        self.assertEqual(models.parse_count("48.2K"), 48200)
        self.assertEqual(models.parse_count("1.2M"), 1200000)
        self.assertEqual(models.parse_count("1,234"), 1234)
        self.assertEqual(models.parse_count(""), 0)
        self.assertEqual(models.parse_count(42), 42)

    def test_downloader_payload_matches_what_api_download_reads(self):
        candidate = models.normalize_candidate(VIDEO_A, handle="emilyintech")
        job = models.job_payload(candidate, creator_id="creator_x", creator_handle="emilyintech")
        video = models.job_to_downloader_video(job)
        # web_app.Api.download 直接读这些键（见 web_app.py 的 download()）
        for field in ("id", "url", "title", "description", "cover", "duration", "type"):
            self.assertIn(field, video)
        self.assertEqual(video["id"], "7312000000000000001")
        self.assertEqual(video["type"], "video")


class PolicyTests(unittest.TestCase):
    def candidate(self, **overrides):
        raw = dict(VIDEO_A)
        raw.update(overrides)
        return models.normalize_candidate(raw, handle="emilyintech")

    def test_new_content_downloads(self):
        decision = DownloadPolicy().decide(self.candidate())
        self.assertTrue(decision.download)
        self.assertEqual(decision.reason, "new")

    def test_library_content_is_skipped(self):
        policy = DownloadPolicy()
        key = "tiktok:7312000000000000001"
        done = policy.decide(self.candidate(), {"library_items": {
            key: {"id": "item_1", "download_status": "done"}}})
        self.assertFalse(done.download)
        self.assertEqual(done.reason, "already_downloaded")
        self.assertEqual(done.content_item_id, "item_1")

        pending = policy.decide(self.candidate(), {"library_items": {
            key: {"id": "item_2", "download_status": "pending"}}})
        self.assertEqual(pending.reason, "in_library")

    def test_failed_library_item_is_retried(self):
        decision = DownloadPolicy().decide(self.candidate(), {"library_items": {
            "tiktok:7312000000000000001": {"id": "item_3", "download_status": "failed"}}})
        self.assertTrue(decision.download)
        self.assertEqual(decision.reason, "retry_failed_item")

    def test_active_job_blocks_the_same_content(self):
        decision = DownloadPolicy().decide(
            self.candidate(), {"active_keys": {"tiktok:7312000000000000001"}})
        self.assertFalse(decision.download)
        self.assertEqual(decision.reason, "job_active")

    def test_retry_stops_after_max_attempts(self):
        key = "tiktok:7312000000000000001"
        fresh = DownloadPolicy(max_attempts=3).decide(
            self.candidate(), {"job_history": {key: {"state": "failed", "attempts": 0}}})
        self.assertTrue(fresh.download)
        self.assertEqual(fresh.reason, "retry_after_failure")

        exhausted = DownloadPolicy(max_attempts=3).decide(
            self.candidate(), {"job_history": {key: {"state": "failed", "attempts": 3}}})
        self.assertFalse(exhausted.download)
        self.assertEqual(exhausted.reason, "attempts_exhausted")

    def test_duration_type_and_keyword_filters(self):
        policy = DownloadPolicy(min_duration=30, max_duration=60,
                                keywords_block=["广告", "sponsored"], include_kinds=["video"])
        self.assertEqual(policy.decide(self.candidate(duration=10)).reason, "too_short")
        self.assertEqual(policy.decide(self.candidate(duration=90)).reason, "too_long")
        self.assertEqual(policy.decide(self.candidate(title="sponsored content")).reason,
                         "keyword_blocked")
        self.assertEqual(policy.decide(self.candidate(
            id="9", url="https://www.tiktok.com/@a/photo/9", type="image",
            title="photo post")).reason, "type_excluded")
        self.assertTrue(policy.decide(self.candidate()).download)

    def test_per_creator_limit(self):
        decision = DownloadPolicy(per_creator_limit=2).decide(self.candidate(),
                                                              {"creator_queued": 2})
        self.assertEqual(decision.reason, "per_creator_limit")

    def test_allow_list_is_off_by_default(self):
        """设置里默认的 keywords_allow（tutorial / how to …）不能变成默认白名单。"""
        self.assertFalse(DownloadPolicy(keywords_allow=["tutorial"]).keywords_allow_enabled)
        self.assertTrue(DownloadPolicy(keywords_allow=["tutorial"]).decide(self.candidate()).download)
        strict = DownloadPolicy(keywords_allow=["tutorial"], keywords_allow_enabled=True)
        self.assertEqual(strict.decide(self.candidate()).reason, "keyword_not_allowed")


class DiscoveryTests(unittest.TestCase):
    def test_downloader_source_reuses_recognize(self):
        class FakeDownloader:
            def __init__(self, payload):
                self.payload = payload
                self.calls = []

            def recognize(self, url):
                self.calls.append(url)
                return self.payload

        downloader = FakeDownloader({"ok": True, "videos": [VIDEO_A], "complete": True,
                                     "avatar": "https://img/a.jpg",
                                     "profileStats": {"followers": "48.2K"}})
        source = DownloaderCandidateSource(downloader)
        result = source.discover({"handle": "emilyintech"})
        self.assertTrue(result.ok)
        self.assertEqual(result.state, "success")
        self.assertEqual(len(result.videos), 1)
        self.assertEqual(downloader.calls, ["https://www.tiktok.com/@emilyintech"])

    def test_downloader_source_reports_failures_instead_of_pretending(self):
        class Broken:
            def recognize(self, url):
                raise RuntimeError("浏览器连接失败")

        failed = DownloaderCandidateSource(Broken()).discover({"handle": "a"})
        self.assertFalse(failed.ok)
        self.assertEqual(failed.state, "failed")
        self.assertIn("浏览器连接失败", failed.error)

        blocked = DownloaderCandidateSource(
            type("D", (), {"recognize": lambda self, url: {"ok": False, "error": "需要安全验证",
                                                           "needsVerification": True}})()
        ).discover({"handle": "a"})
        self.assertFalse(blocked.ok)
        self.assertTrue(blocked.needs_verification)

    def test_missing_downloader_gives_a_clear_error(self):
        result = DownloaderCandidateSource(None).discover({"handle": "a"})
        self.assertFalse(result.ok)
        self.assertIn("recognize", result.error)

    def test_partial_read_is_reported_as_partial(self):
        result = StaticCandidateSource(videos=[VIDEO_A], complete=False,
                                       warning="分页没有前进").discover({"handle": "a"})
        self.assertEqual(result.state, "partial")


class PollTests(TempEnvCase):
    def test_candidates_are_discovered_and_queued(self):
        collector = self.collector()
        result = collector.poll_creator(self.creator_id)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["summary"]["discovered"], 2)
        self.assertEqual(result["summary"]["queued"], 2)

        jobs = collector.pending_jobs()
        self.assertEqual(len(jobs), 2)
        self.assertEqual({job["creator_handle"] for job in jobs}, {"emilyintech"})
        self.assertEqual({job["priority"] for job in jobs}, {"高"})
        self.assertEqual({job["content_key"] for job in jobs},
                         {"tiktok:7312000000000000001", "tiktok:7312000000000000002"})
        self.assertTrue(all(job["state"] == "pending" for job in jobs))

    def test_a_poll_writes_a_success_record_and_moves_the_schedule(self):
        collector = self.collector()
        collector.poll_creator(self.creator_id)
        runs = collector.jobs.runs(creator_id=self.creator_id)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["state"], "success")
        self.assertEqual(runs[0]["discovered"], 2)
        self.assertEqual(runs[0]["queued"], 2)
        self.assertEqual(runs[0]["trigger"], "poll")

        state = collector.monitor.state(self.creator_id)
        self.assertTrue(state["last_check_at"])
        self.assertTrue(state["next_check_at"] > state["last_check_at"])
        self.assertEqual(state["total_checks"], 1)
        self.assertEqual(state["total_queued"], 2)

    def test_repeated_polling_does_not_create_duplicate_jobs(self):
        collector = self.collector()
        first = collector.poll_creator(self.creator_id)
        second = collector.poll_creator(self.creator_id)
        third = collector.poll_creator(self.creator_id)

        self.assertEqual(first["summary"]["queued"], 2)
        self.assertEqual(second["summary"]["queued"], 0)
        self.assertEqual(third["summary"]["queued"], 0)
        self.assertEqual(second["summary"]["duplicates"], 2, "第二次全部识别为队列中已有")
        self.assertEqual(collector.jobs.counts()["total"], 2)
        self.assertEqual(collector.jobs.counts()["pending"], 2)
        self.assertEqual(len(collector.pending_jobs()), 2)

    def test_duplicate_videos_inside_one_batch_are_collapsed(self):
        same_video_twice = [VIDEO_A,
                            dict(VIDEO_A, url=VIDEO_A["url"] + "?is_from_webapp=1&x=2",
                                 title="同一个作品的另一种写法")]
        collector = self.collector(videos=same_video_twice)
        result = collector.poll_creator(self.creator_id)
        self.assertEqual(result["summary"]["discovered"], 2)
        self.assertEqual(result["summary"]["queued"], 1)
        self.assertEqual(result["summary"]["duplicates"], 1)
        self.assertEqual(len(collector.pending_jobs()), 1)

    def test_content_already_in_the_library_is_not_queued_again(self):
        self.store.upsert_item("7312000000000000001", source_type="tiktok",
                               source_url=VIDEO_A["url"], creator_handle="emilyintech",
                               title="已经有了", download_status="done")
        collector = self.collector()
        result = collector.poll_creator(self.creator_id)
        self.assertEqual(result["summary"]["queued"], 1, "已下载的那条不再进任务")
        self.assertEqual(result["summary"]["existing"], 1)
        keys = {job["content_key"] for job in collector.pending_jobs()}
        self.assertEqual(keys, {"tiktok:7312000000000000002"})

    def test_disabled_creator_is_skipped_and_recorded(self):
        collector = self.collector()
        self.monitor.disable(self.creator_id)
        result = collector.poll_creator(self.creator_id)
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "disabled")
        runs = collector.jobs.runs(creator_id=self.creator_id)
        self.assertEqual(runs[0]["state"], "skipped")
        self.assertEqual(collector.jobs.counts()["total"], 0)

    def test_a_failed_read_is_recorded_with_a_reason(self):
        collector = self.collector(error="TikTok 要求安全验证")
        result = collector.poll_creator(self.creator_id)
        self.assertFalse(result["ok"])
        self.assertIn("安全验证", result["error"])

        runs = collector.jobs.runs(creator_id=self.creator_id)
        self.assertEqual(runs[0]["state"], "failed")
        self.assertIn("安全验证", runs[0]["error"])
        row = collector.monitor.creator(self.creator_id)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["last_state"], "failed")
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertIn("安全验证", row["last_error"])
        self.assertFalse(row["is_due"], "失败之后要退避，不能马上再敲一遍")
        self.assertEqual(collector.jobs.counts()["total"], 0)

    def test_an_exploding_source_is_recorded_not_raised(self):
        class Exploding:
            name = "exploding"

            def discover(self, creator, newest_only=False):
                raise RuntimeError("浏览器崩了")

        collector = ContentCollector(self.store, monitor=self.monitor, settings=self.settings,
                                     source=Exploding())
        result = collector.poll_creator(self.creator_id)
        self.assertFalse(result["ok"])
        self.assertIn("浏览器崩了", result["error"])
        self.assertEqual(collector.jobs.runs(creator_id=self.creator_id)[0]["state"], "failed")

    def test_a_second_poll_cannot_run_while_one_is_in_flight(self):
        collector = self.collector()
        self.assertTrue(self.monitor.begin_check(self.creator_id, owner="someone-else"))
        result = collector.poll_creator(self.creator_id)
        self.assertFalse(result["ok"])
        self.assertTrue(result["busy"])
        self.assertEqual(result["reason"], "leased")
        self.assertEqual(collector.jobs.runs(creator_id=self.creator_id)[0]["state"], "busy")
        self.monitor.end_check(self.creator_id, owner="someone-else")
        self.assertTrue(collector.poll_creator(self.creator_id)["ok"])

    def test_check_now_ignores_the_schedule_but_not_the_lock(self):
        collector = self.collector()
        collector.poll_creator(self.creator_id)
        self.assertEqual(collector.monitor.due_creators(), [])
        result = collector.check_now(self.creator_id)
        self.assertTrue(result["ok"], "手动「立即检查」不该被 next_check_at 挡住")
        self.assertEqual(collector.jobs.runs(creator_id=self.creator_id)[0]["trigger"], "manual")

    def test_tick_only_polls_creators_that_are_due(self):
        collector = self.collector()
        second = self.monitor.create_creator("techwithtim")["creatorId"]
        first_tick = collector.tick()
        self.assertEqual(first_tick["checked"], 2)
        self.assertEqual(first_tick["queued"], 2, "只有 emilyintech 有候选项")

        self.monitor.disable(second)
        again = collector.tick()
        self.assertEqual(again["checked"], 0, "都还没到下一次检查时间")

        forced = collector.tick(force=True)
        self.assertEqual(forced["checked"], 1, "强制模式跳过停用的那个")

    def test_profile_stats_are_written_back_to_the_creator_contract(self):
        collector = self.collector(videos=[VIDEO_A], avatar="https://img/e.jpg",
                                   profile_stats={"followers": "48.2K"})
        collector.poll_creator(self.creator_id)
        row = collector.monitor.creator(self.creator_id)
        self.assertEqual(row["avatar"], "https://img/e.jpg")
        self.assertEqual(row["followers"], 48200)

    def test_events_are_emitted_for_the_ui(self):
        collector = self.collector()
        collector.poll_creator(self.creator_id)
        names = [name for name, _payload in self.events]
        self.assertIn("collectProgress", names)
        self.assertIn("collectJob", names)
        progress = [payload for name, payload in self.events if name == "collectProgress"]
        self.assertEqual(progress[-1]["summary"]["queued"], 2)


class JobStoreTests(TempEnvCase):
    def setup_job(self, key="tiktok:1", **fields):
        payload = {"content_key": key, "source_video_id": key.split(":")[-1],
                   "source_url": f"https://www.tiktok.com/@a/video/{key.split(':')[-1]}",
                   "title": "T", "creator_id": "creator_1", "creator_handle": "a"}
        payload.update(fields)
        return payload

    def test_the_same_content_cannot_have_two_open_jobs(self):
        jobs = CollectionJobStore(self.store)
        first = jobs.enqueue(self.setup_job())
        self.assertTrue(first["created"])
        second = jobs.enqueue(self.setup_job())
        self.assertFalse(second["created"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["job"]["id"], first["job"]["id"])
        self.assertEqual(jobs.counts()["total"], 1)

    def test_status_transitions_and_counts(self):
        jobs = CollectionJobStore(self.store)
        job_id = jobs.enqueue(self.setup_job())["job"]["id"]
        self.assertEqual(jobs.counts()["pending"], 1)
        jobs.mark_running(job_id, attempts=1)
        self.assertEqual(jobs.counts()["running"], 1)
        self.assertEqual(jobs.job(job_id)["attempts"], 1)
        jobs.mark_done(job_id, content_item_id="item_9")
        row = jobs.job(job_id)
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["content_item_id"], "item_9")
        self.assertTrue(row["finished_at"])

    def test_a_failed_job_frees_the_slot_for_a_retry(self):
        jobs = CollectionJobStore(self.store)
        job_id = jobs.enqueue(self.setup_job())["job"]["id"]
        jobs.mark_failed(job_id, "网络超时")
        retry = jobs.enqueue(self.setup_job())
        self.assertTrue(retry["created"], "失败过的内容必须还能重试")
        self.assertEqual(jobs.counts()["failed"], 1)
        self.assertEqual(jobs.counts()["pending"], 1)

    def test_history_keeps_the_latest_job_per_content(self):
        jobs = CollectionJobStore(self.store)
        job_id = jobs.enqueue(self.setup_job())["job"]["id"]
        jobs.mark_failed(job_id, "boom")
        jobs.enqueue(self.setup_job(attempts=1))
        history = jobs.history()
        self.assertEqual(history["tiktok:1"]["state"], "pending")
        self.assertEqual(history["tiktok:1"]["attempts"], 1)

    def test_priority_orders_the_queue(self):
        jobs = CollectionJobStore(self.store)
        jobs.enqueue(self.setup_job("tiktok:1", priority="低"))
        jobs.enqueue(self.setup_job("tiktok:2", priority="高"))
        jobs.enqueue(self.setup_job("tiktok:3", priority="中"))
        self.assertEqual([job["source_video_id"] for job in jobs.pending()], ["2", "3", "1"])

    def test_stale_running_jobs_come_back_to_pending(self):
        jobs = CollectionJobStore(self.store)
        job_id = jobs.enqueue(self.setup_job())["job"]["id"]
        jobs.mark_running(job_id)
        self.assertEqual(jobs.requeue_stale(seconds=3600), 0, "刚跑的不能算超时")
        self.assertEqual(jobs.requeue_stale(seconds=-1), 1)
        self.assertEqual(jobs.job(job_id)["state"], "pending")

    def test_deleting_a_creator_cancels_its_open_jobs(self):
        jobs = CollectionJobStore(self.store)
        jobs.enqueue(self.setup_job(creator_id=self.creator_id))
        self.monitor.delete_creator(self.creator_id)
        self.assertEqual(jobs.counts()["cancelled"], 1)
        self.assertEqual(jobs.counts()["pending"], 0)


class ReconcileTests(TempEnvCase):
    def test_jobs_are_closed_from_the_real_library_state(self):
        collector = self.collector(videos=[VIDEO_A, VIDEO_B])
        collector.poll_creator(self.creator_id)
        jobs = collector.pending_jobs()
        self.assertEqual(len(jobs), 2)

        self.store.upsert_item("7312000000000000001", source_type="tiktok",
                               source_url=VIDEO_A["url"], creator_handle="emilyintech",
                               title="下好了", download_status="done")
        self.store.upsert_item("7312000000000000002", source_type="tiktok",
                               source_url=VIDEO_B["url"], creator_handle="emilyintech",
                               title="下坏了", download_status="failed", last_error="网络超时")

        result = collector.reconcile()
        self.assertEqual(result["done"], 1)
        self.assertEqual(result["failed"], 1)
        by_key = {job["content_key"]: job for job in
                  collector.jobs.jobs(states=("done", "failed"), limit=10)}
        finished = by_key["tiktok:7312000000000000001"]
        self.assertEqual(finished["state"], "done")
        self.assertTrue(finished["content_item_id"], "要指回内容库那条记录")
        self.assertEqual(by_key["tiktok:7312000000000000002"]["state"], "failed")
        self.assertIn("网络超时", by_key["tiktok:7312000000000000002"]["last_error"])

    def test_reconcile_is_idempotent(self):
        collector = self.collector(videos=[VIDEO_A])
        collector.poll_creator(self.creator_id)
        self.store.upsert_item("7312000000000000001", source_type="tiktok",
                               source_url=VIDEO_A["url"], creator_handle="emilyintech",
                               download_status="done")
        first = collector.reconcile()
        second = collector.reconcile()
        self.assertEqual(first["done"], 1)
        self.assertEqual(second["checked"], 0)
        self.assertEqual(collector.jobs.counts()["done"], 1)


class HandoffTests(TempEnvCase):
    class FakeDownloader:
        def __init__(self, fail_ids=(), raise_error=None):
            self.fail_ids = set(fail_ids)
            self.raise_error = raise_error
            self.calls = []

        def download(self, videos, folder, quality, retry_count=3, concurrency=1):
            self.calls.append({"videos": videos, "folder": folder, "quality": quality})
            if self.raise_error:
                raise self.raise_error
            return {"ok": len(videos) - len(self.fail_ids),
                    "failed": [{"id": video_id, "error": "模拟下载失败"}
                               for video_id in self.fail_ids],
                    "folder": folder}

    def test_jobs_are_handed_to_the_downloader_in_its_own_shape(self):
        collector = self.collector()
        collector.poll_creator(self.creator_id)
        downloader = self.FakeDownloader()
        self.settings.update("storage", {"video_path": str(self.root / "downloads")})

        result = download_jobs(collector, downloader)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["downloaded"], 2)
        call = downloader.calls[0]
        self.assertEqual(call["quality"], self.settings.section("collect")["download_quality"])
        self.assertTrue(call["folder"].endswith("downloads"))
        self.assertEqual([video["id"] for video in call["videos"]],
                         ["7312000000000000001", "7312000000000000002"])
        self.assertEqual(collector.jobs.counts()["done"], 2)
        self.assertEqual(collector.jobs.counts()["pending"], 0)

    def test_failed_downloads_are_recorded_on_the_job(self):
        collector = self.collector(videos=[VIDEO_A])
        collector.poll_creator(self.creator_id)
        downloader = self.FakeDownloader(fail_ids=["7312000000000000001"])
        result = download_jobs(collector, downloader,
                               folder=str(self.root / "downloads"))
        self.assertEqual(result["failedCount"], 1)
        failed = collector.jobs.failed_jobs()
        self.assertEqual(len(failed), 1)
        self.assertIn("模拟下载失败", failed[0]["last_error"])

    def test_a_downloader_crash_does_not_take_the_collector_down(self):
        collector = self.collector(videos=[VIDEO_A])
        collector.poll_creator(self.creator_id)
        downloader = self.FakeDownloader(raise_error=RuntimeError("yt-dlp 崩了"))
        result = download_jobs(collector, downloader, folder=str(self.root / "downloads"))
        self.assertFalse(result["ok"])
        self.assertIn("yt-dlp 崩了", result["error"])
        self.assertEqual(collector.jobs.counts()["failed"], 1)
        self.assertEqual(collector.jobs.counts()["running"], 0, "不能有任务卡在 running")

    def test_missing_folder_and_missing_downloader_are_explained(self):
        collector = self.collector(videos=[VIDEO_A])
        collector.poll_creator(self.creator_id)
        no_folder = download_jobs(collector, self.FakeDownloader())
        self.assertFalse(no_folder["ok"])
        self.assertIn("下载目录", no_folder["error"])
        no_downloader = download_jobs(collector, None, folder=str(self.root))
        self.assertFalse(no_downloader["ok"])
        self.assertIn("下载器", no_downloader["error"])
        self.assertEqual(collector.jobs.counts()["pending"], 1, "任务不能被这两次失败吃掉")

    def test_nothing_to_do_is_not_an_error(self):
        collector = self.collector(videos=[])
        collector.poll_creator(self.creator_id)
        result = download_jobs(collector, self.FakeDownloader(), folder=str(self.root))
        self.assertTrue(result["ok"])
        self.assertEqual(result["downloaded"], 0)
        self.assertIn("没有待下载", result["message"])

    def test_failed_jobs_are_retried_on_the_next_poll_and_then_given_up(self):
        """失败 → 下次 polling 自动重排；重试次数用尽后不再打扰下载器。"""
        collector = self.collector(videos=[VIDEO_A])
        attempts = 0
        for _ in range(6):
            poll = collector.poll_creator(self.creator_id)
            pending = collector.pending_jobs()
            if not pending:
                self.assertEqual(poll["summary"]["queued"], 0)
                break
            attempts += 1
            job = pending[0]
            self.assertEqual(job["attempts"], attempts - 1, "重试任务继承已经尝试过的次数")
            collector.jobs.mark_running(job["id"], attempts=job["attempts"] + 1)
            collector.jobs.mark_failed(job["id"], "又一次失败")

        self.assertEqual(attempts, 3, "初次下载 + 两次重试；第三次失败后不再排队")
        self.assertEqual(collector.jobs.counts()["pending"], 0)
        self.assertEqual(collector.jobs.counts()["failed"], 3)
        reasons = [row["reason"] for row in
                   collector.poll_creator(self.creator_id)["decisions"]
                   if not row["download"]]
        self.assertIn("attempts_exhausted", reasons)


class RunnerTests(TempEnvCase):
    def test_runner_ticks_and_stops(self):
        collector = self.collector(videos=[VIDEO_A])
        runner = CollectorRunner(collector, interval=5)
        self.assertFalse(runner.running)
        first = runner.run_once()
        self.assertEqual(first["checked"], 1)
        self.assertEqual(collector.jobs.counts()["pending"], 1)
        self.assertEqual(runner.status()["rounds"], 1)

        runner.start()
        self.assertTrue(runner.running)
        deadline = time.time() + 20
        while runner.status()["rounds"] < 2 and time.time() < deadline:
            time.sleep(0.2)
        runner.stop()
        self.assertFalse(runner.running)
        self.assertGreaterEqual(runner.status()["rounds"], 2)

    def test_runner_interval_comes_from_settings(self):
        self.settings.update("work_mode", {"task_interval_sec": 120})
        runner = CollectorRunner.from_settings(self.collector(videos=[]), self.settings)
        self.assertEqual(runner.interval, 120)


class ViewTests(TempEnvCase):
    def test_status_plan_and_views_shape(self):
        collector = self.collector(videos=[VIDEO_A])
        collector.poll_creator(self.creator_id)
        status = collector.status()
        self.assertEqual(status["jobs"]["pending"], 1)
        self.assertEqual(status["creators"]["total"], 1)
        self.assertEqual(status["source"], "static")
        self.assertEqual(status["policy"]["maxAttempts"], 3)

        plan = collector.plan()
        self.assertEqual(plan["count"], 1)
        self.assertEqual(plan["videos"][0]["id"], "7312000000000000001")
        self.assertEqual(plan["jobs"][0]["state"], "pending", "plan 不改状态")

        claimed = collector.claim()
        self.assertEqual(claimed["jobs"][0]["state"], "running")
        self.assertEqual(claimed["jobs"][0]["attempts"], 1)
        self.assertEqual(collector.jobs_view()["counts"]["running"], 1)
        self.assertEqual(len(collector.runs_view()["runs"]), 1)
        self.assertEqual(collector.failures_view()["jobs"], [])

    def test_build_collector_without_a_downloader_fails_loudly(self):
        collector = build_collector(self.store, downloader=None, settings=self.settings)
        result = collector.poll_creator(self.creator_id)
        self.assertFalse(result["ok"])
        self.assertIn("下载器", result["error"])


class RealDownloaderHandoffTests(unittest.TestCase):
    """采集任务交给**真正的** Api.download：形状不对这里就会炸。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_end_to_end_with_the_real_downloader(self):
        from web_app import Api

        class FakeYdl:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def download(self, urls):
                template = self.options["outtmpl"].replace(".%(ext)s", "")
                base = (template.replace("%(title).150B", "clip")
                        .replace("%(upload_date)s", "20260115").replace("%(title)s", "clip"))
                Path(base + ".mp4").write_bytes(b"fake video bytes")
                Path(base + ".en.srt").write_text(
                    "1\n00:00:01,000 --> 00:00:03,000\nhello from the collector\n",
                    encoding="utf-8")
                hook = (self.options.get("progress_hooks") or [None])[0]
                if hook:
                    hook({"status": "finished", "filename": base + ".mp4"})

        api = Api()
        store = FactoryStore()                       # 与下载器桥接层同一个库文件
        settings = FactorySettings(self.root)
        settings.update("storage", {"video_path": str(self.root / "downloads")})
        settings.update("collect", {"keywords_block": []})
        monitor = CreatorMonitorService(store, settings)
        creator_id = monitor.create_creator("emilyintech", priority="高")["creatorId"]
        source = StaticCandidateSource(videos=[VIDEO_A], complete=True)
        collector = build_collector(store, downloader=api, settings=settings, source=source)

        poll = collector.poll_creator(creator_id)
        self.assertEqual(poll["summary"]["queued"], 1)

        with patch("web_app.Api._youtube_dl", lambda self, options: FakeYdl(options)), \
                patch("web_app.Api._photo_urls", lambda self, item: []):
            result = download_jobs(collector, api, retry_count=0)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["downloaded"], 1)

        # 下载器自己走原来的入库链路（content_register_download），采集器只是读结果
        rows = [row for row in api.content_factory.content_items(status="all", limit=10)["items"]
                if row["source_type"] == "tiktok"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_video_id"], "7312000000000000001")
        self.assertEqual(rows[0]["download_status"], "done")

        job = collector.jobs.jobs(states=("done",), limit=5)[0]
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["content_item_id"], rows[0]["id"])
        self.assertEqual(collector.jobs.counts()["pending"], 0)

    def test_real_downloader_metadata_flows_into_candidates(self):
        """下载器 recognize() 的结果要能被归一化成候选（键名对得上）。"""
        payload = {"ok": True, "complete": True, "username": "emilyintech",
                   "avatar": "https://img/e.jpg", "profileStats": {"followers": "48.2K"},
                   "videos": [VIDEO_A, dict(VIDEO_B, views="12.3K")]}
        source = DownloaderCandidateSource(type("D", (), {"recognize": lambda self, url: payload})())
        result = source.discover({"handle": "emilyintech"})
        candidates = models.normalize_candidates(result.videos, handle="emilyintech")
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0].content_key, "tiktok:7312000000000000001")
        self.assertEqual(models.parse_count(result.profile_stats["followers"]), 48200)
        self.assertEqual(
            json.loads(json.dumps([candidate.to_dict() for candidate in candidates]))[1]["views"],
            "12.3K")


if __name__ == "__main__":
    unittest.main()
