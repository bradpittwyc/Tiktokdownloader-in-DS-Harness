"""Creator Monitor 的回归网：Creator 增删改查 / 唯一 handle / 启用停用 / 检查节奏。

锁住的行为都是验收项：
- handle 唯一（且忽略大小写：@Emily 与 @emily 是同一个人）
- 停用的 Creator 不再被检查；重新启用后立刻到期
- poll_interval 支持界面上的各种写法，并真的决定 next_check_at
- 失败要留痕：状态、原因、连续失败计数、退避后的下次检查时间
- 同一个 Creator 不会被并发检查两次（租约 + 进程内锁）

全部离线、全部用假时钟：不碰网络，也不靠 sleep 猜时间。
"""

import datetime
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "outputs/TikTokBatchMVP"))

from content_factory.creator_monitor import intervals  # noqa: E402
from content_factory.creator_monitor.service import CreatorMonitorService  # noqa: E402
from content_factory.factory_store import FactoryStore  # noqa: E402
from content_factory.settings_store import FactorySettings  # noqa: E402

STAMP = "%Y-%m-%d %H:%M:%S"


class FakeClock:
    """可控时钟：定时逻辑必须能在测试里瞬间走完，而不是真的等 30 分钟。"""

    def __init__(self, text="2026-01-15 10:00:00"):
        self.moment = datetime.datetime.strptime(text, STAMP)

    def __call__(self):
        return self.moment.strftime(STAMP)

    def advance(self, seconds):
        self.moment += datetime.timedelta(seconds=seconds)
        return self()


class IntervalTests(unittest.TestCase):
    def test_interface_wording_is_understood(self):
        self.assertEqual(intervals.parse_interval("30 分钟"), 1800)
        self.assertEqual(intervals.parse_interval("30分钟"), 1800)
        self.assertEqual(intervals.parse_interval("30m"), 1800)
        self.assertEqual(intervals.parse_interval("1 小时"), 3600)
        self.assertEqual(intervals.parse_interval("2h"), 7200)
        self.assertEqual(intervals.parse_interval("1.5 小时"), 5400)
        self.assertEqual(intervals.parse_interval("1 天"), 86400)
        self.assertEqual(intervals.parse_interval(900), 900)
        self.assertEqual(intervals.parse_interval("900"), 900)
        self.assertEqual(intervals.parse_interval("45 分钟"), 2700)

    def test_bare_numbers_mean_minutes_until_they_are_obviously_seconds(self):
        self.assertEqual(intervals.parse_interval("30"), 1800)
        self.assertEqual(intervals.parse_interval("3600"), 3600)

    def test_garbage_falls_back_to_the_default(self):
        self.assertEqual(intervals.parse_interval("随便写点什么", 1800), 1800)
        self.assertEqual(intervals.parse_interval("", 1800), 1800)
        self.assertEqual(intervals.parse_interval(None, 1800), 1800)

    def test_values_are_clamped_to_a_sane_range(self):
        self.assertEqual(intervals.parse_interval("1 秒"), intervals.MIN_INTERVAL_SECONDS)
        self.assertEqual(intervals.parse_interval("30 天"), intervals.MAX_INTERVAL_SECONDS)

    def test_format_round_trips(self):
        for text, seconds in intervals.INTERVAL_CHOICES:
            self.assertEqual(intervals.format_interval(seconds), text)
            self.assertEqual(intervals.parse_interval(text), seconds)

    def test_priority_is_normalized(self):
        self.assertEqual(intervals.normalize_priority("高"), "高")
        self.assertEqual(intervals.normalize_priority("HIGH"), "高")
        self.assertEqual(intervals.normalize_priority("low"), "低")
        self.assertEqual(intervals.normalize_priority(""), "中")
        self.assertLess(intervals.priority_weight("高"), intervals.priority_weight("低"))

    def test_due_handles_empty_and_shift(self):
        self.assertTrue(intervals.is_due(""), "从没检查过 = 立刻可查")
        self.assertFalse(intervals.is_due("2099-01-01 00:00:00"))
        self.assertEqual(intervals.shift("2026-01-15 10:00:00", 1800), "2026-01-15 10:30:00")


class TempEnvCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LOCALAPPDATA": self.temp.name})
        self.env.start()
        self.root = Path(self.temp.name)
        self.store = FactoryStore(self.root / "content-factory.db")
        self.settings = FactorySettings(self.root)
        self.clock = FakeClock()
        self.monitor = CreatorMonitorService(self.store, self.settings, now=self.clock)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()


class CreatorCrudTests(TempEnvCase):
    def test_creator_fields_follow_the_existing_contract(self):
        result = self.monitor.create_creator("emilyintech", display_name="Emily Zhang",
                                             category="AI 科技", priority="高",
                                             poll_interval="30 分钟", followers=48200, videos=132)
        self.assertTrue(result["ok"], result)
        creator = result["creator"]
        for field in ("id", "handle", "display_name", "avatar", "category", "status",
                      "followers", "videos", "priority", "poll_interval",
                      "created_at", "updated_at"):
            self.assertIn(field, creator, f"Creator contract 缺字段：{field}")
        self.assertEqual(creator["handle"], "emilyintech")
        self.assertEqual(creator["priority"], "高")
        self.assertEqual(creator["followers"], 48200)
        # 监控状态是叠加在契约字段上的，没有覆盖原有字段
        self.assertTrue(creator["enabled"])
        self.assertEqual(creator["poll_interval_seconds"], 1800)
        self.assertTrue(creator["is_due"], "新建的 Creator 立刻该被检查")

    def test_handle_is_unique_and_case_insensitive(self):
        first = self.monitor.create_creator("emilyintech")
        self.assertTrue(first["ok"])
        again = self.monitor.create_creator("emilyintech")
        self.assertFalse(again["ok"])
        self.assertTrue(again["duplicate"])
        self.assertIn("已存在", again["error"])
        upper = self.monitor.create_creator("@EmilyInTech")
        self.assertFalse(upper["ok"], "@EmilyInTech 与 @emilyintech 是同一个人")
        self.assertEqual(len(self.monitor.creators()), 1)
        self.assertEqual(len(self.store.creators()), 1)

    def test_handle_is_validated(self):
        self.assertFalse(self.monitor.create_creator("")["ok"])
        self.assertFalse(self.monitor.create_creator("bad handle!")["ok"])
        self.assertTrue(self.monitor.create_creator("@good.handle_1")["ok"])

    def test_only_one_creator_row_is_created_per_handle(self):
        """监控器建过 Creator 之后，下载链路按 handle 入库必须复用同一行。"""
        created = self.monitor.create_creator("techwithtim", display_name="Tim")
        self.store.upsert_item("7312", creator_handle="techwithtim", title="A")
        self.assertEqual(len(self.store.creators()), 1)
        self.assertEqual(self.monitor.creator(created["creatorId"])["id"], created["creatorId"])

    def test_update_and_delete(self):
        creator_id = self.monitor.create_creator("aliabdaal")["creatorId"]
        updated = self.monitor.update_creator(creator_id, display_name="Ali Abdaal",
                                              category="效率", followers=880000)
        self.assertTrue(updated["ok"])
        row = self.monitor.creator(creator_id)
        self.assertEqual(row["display_name"], "Ali Abdaal")
        self.assertEqual(row["followers"], 880000)

        renamed = self.monitor.update_creator(creator_id, handle="aliabdaal2")
        self.assertTrue(renamed["ok"])
        self.assertEqual(self.monitor.creator(creator_id)["handle"], "aliabdaal2")

        other = self.monitor.create_creator("someoneelse")["creatorId"]
        clash = self.monitor.update_creator(other, handle="aliabdaal2")
        self.assertFalse(clash["ok"])
        self.assertTrue(clash["duplicate"])

        removed = self.monitor.delete_creator(creator_id)
        self.assertTrue(removed["ok"])
        self.assertIsNone(self.monitor.creator(creator_id))
        self.assertIsNone(self.monitor.state(creator_id))
        self.assertEqual(self.monitor.update_creator(creator_id, display_name="x")["ok"], False)

    def test_listing_filters_and_search(self):
        self.monitor.create_creator("emilyintech", category="AI 科技")
        self.monitor.create_creator("aliabdaal", category="效率")
        self.assertEqual(len(self.monitor.creators()), 2)
        self.assertEqual([row["handle"] for row in self.monitor.creators(category="效率")],
                         ["aliabdaal"])
        self.assertEqual([row["handle"] for row in self.monitor.creators(search="emily")],
                         ["emilyintech"])

    def test_stats_counts_everything(self):
        self.monitor.create_creator("a1")
        disabled = self.monitor.create_creator("a2")["creatorId"]
        self.monitor.disable(disabled)
        stats = self.monitor.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["enabled"], 1)
        self.assertEqual(stats["disabled"], 1)
        self.assertEqual(stats["due"], 1, "停用的不算「到期」")


class EnableDisableTests(TempEnvCase):
    def test_disabled_creator_drops_out_of_the_due_list(self):
        creator_id = self.monitor.create_creator("emilyintech")["creatorId"]
        self.assertEqual(len(self.monitor.due_creators()), 1)
        result = self.monitor.disable(creator_id)
        self.assertTrue(result["ok"])
        self.assertFalse(result["creator"]["enabled"])
        self.assertEqual(result["creator"]["status"], "paused", "契约里的 status 要跟着变")
        self.assertEqual(self.monitor.due_creators(), [])

    def test_enabling_makes_it_due_immediately_again(self):
        creator_id = self.monitor.create_creator("emilyintech")["creatorId"]
        self.monitor.mark_checked(creator_id, state="success", discovered=1, queued=1)
        self.clock.advance(60)
        self.assertFalse(self.monitor.creator(creator_id)["is_due"], "刚检查完，还没到点")

        self.monitor.disable(creator_id)
        enabled = self.monitor.enable(creator_id)
        self.assertTrue(enabled["creator"]["enabled"])
        self.assertEqual(enabled["creator"]["status"], "active")
        self.assertTrue(enabled["creator"]["is_due"], "用户点启用 = 想马上看到结果")
        self.assertEqual(len(self.monitor.due_creators()), 1)


class PollIntervalTests(TempEnvCase):
    def test_interval_drives_next_check_time(self):
        creator_id = self.monitor.create_creator("emilyintech", poll_interval="30 分钟")["creatorId"]
        self.monitor.mark_checked(creator_id, state="success")
        row = self.monitor.creator(creator_id)
        self.assertEqual(row["next_check_at"], "2026-01-15 10:30:00")
        self.assertFalse(row["is_due"])

        self.clock.advance(1799)
        self.assertEqual(self.monitor.due_creators(), [])
        self.clock.advance(1)
        self.assertEqual([item["handle"] for item in self.monitor.due_creators()], ["emilyintech"])

    def test_setting_the_interval_updates_both_columns(self):
        creator_id = self.monitor.create_creator("emilyintech")["creatorId"]
        result = self.monitor.set_poll_interval(creator_id, "6 小时")
        self.assertTrue(result["ok"])
        self.assertEqual(result["pollIntervalSeconds"], 21600)
        self.assertEqual(self.monitor.creator(creator_id)["poll_interval"], "6 小时",
                         "Creator contract 里的 poll_interval 文本也要更新")
        self.assertEqual(self.monitor.state(creator_id)["poll_interval_seconds"], 21600)

        self.monitor.mark_checked(creator_id, state="success")
        self.assertEqual(self.monitor.creator(creator_id)["next_check_at"], "2026-01-15 16:00:00")

    def test_contract_text_is_respected_when_nothing_explicit_was_set(self):
        """老 Creator 只有 poll_interval 文本（没有监控状态行）时也要能排期。"""
        creator_id = self.store.upsert_creator("legacyuser", poll_interval="2 小时")
        row = self.monitor.creator(creator_id)
        self.assertEqual(row["poll_interval_seconds"], 7200)
        self.assertTrue(row["enabled"], "升级上来的 Creator 默认启用")

    def test_default_interval_comes_from_settings(self):
        self.settings.update("collect", {"interval_minutes": 15})
        creator_id = self.monitor.create_creator("freshuser")["creatorId"]
        self.assertEqual(self.monitor.creator(creator_id)["poll_interval_seconds"], 900)

    def test_priority_orders_the_due_list(self):
        self.monitor.create_creator("low_guy", priority="低")
        self.monitor.create_creator("high_guy", priority="高")
        self.monitor.create_creator("mid_guy", priority="中")
        self.assertEqual([row["handle"] for row in self.monitor.due_creators()],
                         ["high_guy", "mid_guy", "low_guy"])

    def test_recording_a_check_moves_the_schedule_forward(self):
        creator_id = self.monitor.create_creator("emilyintech", poll_interval="1 小时")["creatorId"]
        self.monitor.mark_checked(creator_id, state="success", discovered=12, queued=3)
        state = self.monitor.state(creator_id)
        self.assertEqual(state["last_check_at"], "2026-01-15 10:00:00")
        self.assertEqual(state["next_check_at"], "2026-01-15 11:00:00")
        self.assertEqual(state["last_discovered"], 12)
        self.assertEqual(state["last_queued"], 3)
        self.assertEqual(state["total_checks"], 1)
        self.assertEqual(state["total_queued"], 3)


class FailureRecordingTests(TempEnvCase):
    def test_failure_sets_status_error_and_backs_off(self):
        creator_id = self.monitor.create_creator("emilyintech", poll_interval="1 小时")["creatorId"]
        result = self.monitor.mark_checked(creator_id, state="failed", error="TikTok 没有返回可读取的作品")
        self.assertTrue(result["ok"])
        self.assertEqual(result["consecutiveFailures"], 1)
        self.assertEqual(result["nextCheckAt"], "2026-01-15 12:00:00", "失败第一次退避到 2 倍间隔")
        row = self.monitor.creator(creator_id)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["last_state"], "failed")
        self.assertIn("没有返回可读取的作品", row["last_error"])

    def test_backoff_grows_then_caps_and_recovers(self):
        creator_id = self.monitor.create_creator("emilyintech", poll_interval="1 小时")["creatorId"]
        waits = []
        for _ in range(6):
            self.monitor.mark_checked(creator_id, state="failed", error="boom")
            state = self.monitor.state(creator_id)
            waited = (intervals.parse_stamp(state["next_check_at"])
                      - intervals.parse_stamp(state["last_check_at"])).total_seconds()
            waits.append(int(waited))
        self.assertEqual(waits, [7200, 14400, 21600, 21600, 21600, 21600],
                         "每次失败翻倍，封顶 6 小时，不能无限增长")
        self.assertEqual(self.monitor.state(creator_id)["consecutive_failures"], 6)

        self.monitor.mark_checked(creator_id, state="success")
        self.assertEqual(self.monitor.state(creator_id)["consecutive_failures"], 0)
        self.assertEqual(self.monitor.creator(creator_id)["status"], "active")
        self.assertEqual(self.monitor.creator(creator_id)["last_error"], "")
        self.assertEqual(self.monitor.creator(creator_id)["next_check_at"], "2026-01-15 11:00:00",
                         "恢复成功后回到正常间隔")

    def test_partial_read_keeps_the_normal_interval(self):
        creator_id = self.monitor.create_creator("emilyintech", poll_interval="1 小时")["creatorId"]
        self.monitor.mark_checked(creator_id, state="partial", error="没抓完")
        self.assertEqual(self.monitor.creator(creator_id)["next_check_at"], "2026-01-15 11:00:00")
        self.assertEqual(self.monitor.state(creator_id)["consecutive_failures"], 0)

    def test_unknown_creator_does_not_explode(self):
        self.assertFalse(self.monitor.mark_checked("creator_missing")["ok"])
        self.assertFalse(self.monitor.set_enabled("creator_missing", True)["ok"])
        self.assertFalse(self.monitor.set_poll_interval("creator_missing", "1 小时")["ok"])
        self.assertIsNone(self.monitor.creator("creator_missing"))


class ReentrancyTests(TempEnvCase):
    def test_a_second_check_cannot_start_while_one_is_running(self):
        creator_id = self.monitor.create_creator("emilyintech")["creatorId"]
        self.assertTrue(self.monitor.begin_check(creator_id, owner="worker-a"))
        self.assertTrue(self.monitor.is_checking(creator_id))
        self.assertFalse(self.monitor.begin_check(creator_id, owner="worker-b"),
                         "同一个 Creator 同时只能有一次检查")
        self.monitor.end_check(creator_id, owner="worker-a")
        self.assertFalse(self.monitor.is_checking(creator_id))
        self.assertTrue(self.monitor.begin_check(creator_id, owner="worker-b"))

    def test_expired_lease_is_reclaimed(self):
        """进程被杀留下的死租约必须能自动回收，否则这个 Creator 永远不再被检查。"""
        creator_id = self.monitor.create_creator("emilyintech")["creatorId"]
        self.assertTrue(self.monitor.begin_check(creator_id, owner="dead-worker", lease_seconds=60))
        self.assertFalse(self.monitor.begin_check(creator_id, owner="new-worker", lease_seconds=60))
        self.clock.advance(61)
        self.assertTrue(self.monitor.begin_check(creator_id, owner="new-worker", lease_seconds=60))

    def test_release_only_clears_its_own_lease(self):
        creator_id = self.monitor.create_creator("emilyintech")["creatorId"]
        self.monitor.begin_check(creator_id, owner="worker-a")
        self.monitor.end_check(creator_id, owner="someone-else")
        self.assertTrue(self.monitor.is_checking(creator_id), "别人的租约不能被误清")
        self.monitor.end_check(creator_id, owner="worker-a")
        self.assertFalse(self.monitor.is_checking(creator_id))

    def test_expire_leases_clears_everything_stale(self):
        creator_id = self.monitor.create_creator("emilyintech")["creatorId"]
        self.monitor.begin_check(creator_id, owner="worker-a", lease_seconds=60)
        self.clock.advance(120)
        self.assertEqual(self.monitor.expire_leases(lease_seconds=60), 1)
        self.assertFalse(self.monitor.is_checking(creator_id))


class PersistenceTests(TempEnvCase):
    def test_monitor_state_survives_a_restart(self):
        creator_id = self.monitor.create_creator("emilyintech", poll_interval="30 分钟")["creatorId"]
        self.monitor.disable(creator_id)
        self.monitor.mark_checked(creator_id, state="failed", error="网络超时")

        reopened = CreatorMonitorService(FactoryStore(self.root / "content-factory.db"),
                                         FactorySettings(self.root), now=self.clock)
        row = reopened.creator(creator_id)
        self.assertFalse(row["enabled"])
        self.assertEqual(row["poll_interval_seconds"], 1800)
        self.assertEqual(row["last_state"], "failed")
        self.assertIn("网络超时", row["last_error"])
        self.assertEqual(row["consecutive_failures"], 1)

    def test_creators_created_by_the_downloader_are_picked_up(self):
        """下载器按 handle 建出来的 Creator（老路径）也必须能被监控。"""
        creator_id = self.store.upsert_creator("techwithtim", display_name="Tim")
        self.assertIn(creator_id, [row["id"] for row in self.monitor.creators()])
        self.assertIn(creator_id, [row["id"] for row in self.monitor.due_creators()])


if __name__ == "__main__":
    unittest.main()
