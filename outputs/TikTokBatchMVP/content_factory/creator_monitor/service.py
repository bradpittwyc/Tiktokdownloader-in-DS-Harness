"""Creator Monitor：创作者的增删改查 + 检查节奏 + 下一次检查时间。

职责边界（这是本模块存在的理由）：
- 这里只管「谁要被检查、多久检查一次、上次检查结果如何」，
  **不碰**任何抓取 / 下载逻辑，也不 import 下载器。
- Creator 的基础字段仍然写进原来的 creators 表、沿用原来的 contract；
  只有监控状态（启用开关 / 上次检查 / 下次检查 / 连续失败）落在
  creator_monitor_state（见 store.py 顶部的说明）。
- handle 唯一：查重时忽略大小写（@Emily 与 @emily 是同一个人），
  否则同一个创作者会被抓两遍、任务也会重复。
"""

import datetime
import re
import sqlite3

from . import intervals
from .store import CreatorMonitorStore

HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# 失败后的退避上限：连续失败最多退到 6 小时一次，仍然会自动重试，
# 不会像「一失败就永久停用」那样把创作者悄悄丢掉。
MAX_BACKOFF_SECONDS = 21600

CREATOR_EDITABLE = ("display_name", "avatar", "category", "status", "followers",
                    "videos", "priority", "poll_interval")

# 界面 / JS 习惯用的驼峰名（不翻译的话就会「成功但什么都没改」）
FIELD_ALIASES = {
    "displayName": "display_name",
    "pollInterval": "poll_interval",
    "pollIntervalSeconds": "poll_interval_seconds",
    "pollIntervalText": "poll_interval",
    "followerCount": "followers",
    "videoCount": "videos",
}

# 这些键由调用方自己处理（不是 Creator 字段，也不算「写错了」）
IGNORED_KEYS = ("id", "creatorId", "handle", "enabled", "count", "limit")


class CreatorMonitorService:
    def __init__(self, store, settings=None, now=None):
        self.store = store
        self.settings = settings
        self._now = now or intervals.now_text
        self.monitor = CreatorMonitorStore(store, now=now)

    # ---- 内部 ----------------------------------------------------------
    def _stamp(self):
        return intervals.stamp(self._now())

    def _moment(self):
        """当前时刻的 datetime（用于「到点了吗」这类比较）。"""
        return intervals.parse_stamp(self._stamp()) or datetime.datetime.now()

    def default_interval_seconds(self):
        """没有显式设置时的默认节奏：优先读「设置 → 采集」里的 interval_minutes。"""
        minutes = None
        if self.settings is not None:
            try:
                minutes = int(self.settings.get("collect", "interval_minutes"))
            except (TypeError, ValueError):
                minutes = None
        if minutes and minutes > 0:
            return intervals.clamp_seconds(minutes * 60)
        return intervals.DEFAULT_INTERVAL_SECONDS

    def _decorate(self, row):
        if not row:
            return row
        row = dict(row)
        explicit = 0
        try:
            explicit = int(row.get("poll_interval_seconds") or 0)
        except (TypeError, ValueError):
            explicit = 0
        seconds = (intervals.clamp_seconds(explicit) if explicit > 0
                   else intervals.parse_interval(row.get("poll_interval"),
                                                 self.default_interval_seconds()))
        row["poll_interval_seconds"] = seconds
        row["enabled"] = bool(row.get("enabled", 1))
        row["priority"] = intervals.normalize_priority(row.get("priority"))
        row["priority_weight"] = intervals.priority_weight(row["priority"])
        moment = self._moment()
        row["is_due"] = bool(row["enabled"]) and intervals.is_due(row.get("next_check_at"), moment)
        row["due_in_seconds"] = intervals.seconds_until(row.get("next_check_at"), moment)
        row["checking"] = bool(str(row.get("running_since") or "").strip())
        row["poll_interval_text"] = intervals.format_interval(seconds)
        return row

    def _creator_or_error(self, creator_id):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return None
        self.monitor.state(row["id"])           # 老 Creator 没有状态行时补一行
        return self.monitor.creators_with_state("c.id=?", (row["id"],), limit=1)[0]

    # ---- Creator CRUD --------------------------------------------------
    def create_creator(self, handle, **fields):
        text = str(handle or "").strip().lstrip("@")
        if not text:
            return {"ok": False, "error": "handle 不能为空"}
        if not HANDLE_PATTERN.match(text):
            return {"ok": False, "error": f"handle 不合法：{text}（只允许字母、数字、. _ -）"}
        fields, ignored = _normalize_fields(fields)
        existing = self.monitor.find_by_handle(text)
        if existing:
            return {"ok": False, "duplicate": True, "creatorId": existing["id"],
                    "error": f"创作者 @{existing['handle']} 已存在",
                    "creator": self.creator(existing["id"])}

        priority = intervals.normalize_priority(fields.get("priority"))
        explicit = fields.get("poll_interval_seconds")
        if explicit:
            seconds = intervals.parse_interval(explicit, self.default_interval_seconds())
        elif fields.get("poll_interval"):
            seconds = intervals.parse_interval(fields["poll_interval"], self.default_interval_seconds())
        else:
            seconds = self.default_interval_seconds()

        from content_factory.factory_store import new_id
        creator_id = new_id("creator")
        payload = {key: value for key, value in fields.items() if key in CREATOR_EDITABLE}
        payload["priority"] = priority
        payload["poll_interval"] = intervals.format_interval(seconds)
        try:
            self.monitor.create_creator_with_state(
                creator_id, text, payload,
                {"enabled": 1 if fields.get("enabled", True) else 0,
                 "poll_interval_seconds": seconds,
                 "next_check_at": self._stamp()})       # 新加的创作者立刻排一次检查
        except sqlite3.IntegrityError:
            # 两个线程同时建同一个 handle：creators.handle 的 UNIQUE 兜住了，
            # 对调用方来说这仍然是「已存在」，不该看到数据库异常。
            existing = self.monitor.find_by_handle(text)
            return {"ok": False, "duplicate": True,
                    "creatorId": (existing or {}).get("id", ""),
                    "error": f"创作者 @{text} 已存在",
                    "creator": self.creator(existing["id"]) if existing else None}
        return {"ok": True, "created": True, "creatorId": creator_id,
                "ignored": ignored, "creator": self.creator(creator_id)}

    def update_creator(self, creator_id, **fields):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return {"ok": False, "error": "创作者不存在"}
        fields, ignored = _normalize_fields(fields)
        payload = {}
        for key, value in fields.items():
            if key not in CREATOR_EDITABLE or value is None:
                continue
            if key == "priority":
                value = intervals.normalize_priority(value)
            payload[key] = value
        new_handle = str(fields.get("handle") or "").strip().lstrip("@")
        if new_handle and new_handle != row["handle"]:
            if not HANDLE_PATTERN.match(new_handle):
                return {"ok": False, "error": f"handle 不合法：{new_handle}"}
            clash = self.monitor.find_by_handle(new_handle)
            if clash and clash["id"] != row["id"]:
                return {"ok": False, "duplicate": True, "error": f"创作者 @{new_handle} 已存在"}
            self.monitor.update_handle(row["id"], new_handle)
        state_fields = {}
        explicit = fields.get("poll_interval_seconds")
        if explicit is not None and explicit != "":
            seconds = intervals.parse_interval(explicit, self.default_interval_seconds())
            state_fields["poll_interval_seconds"] = seconds
            payload["poll_interval"] = intervals.format_interval(seconds)
        if "poll_interval" in payload:
            seconds = intervals.parse_interval(payload["poll_interval"], self.default_interval_seconds())
            payload["poll_interval"] = intervals.format_interval(seconds)
            state_fields["poll_interval_seconds"] = seconds
        changed = self.monitor.save_state_and_creator(row["id"], state_fields=state_fields,
                                                      creator_fields=payload)
        result = {"ok": True, "changed": changed or (1 if new_handle else 0),
                  "creator": self.creator(row["id"])}
        if ignored:
            # 认不出的字段要报出来：界面写错一个字段名却收到 ok=true，
            # 会变成「保存成功但什么都没变」这种最难查的问题。
            result["ignored"] = ignored
        if not payload and not new_handle and not ignored:
            result["message"] = "没有需要更新的字段"
        return result

    def delete_creator(self, creator_id, cancel_jobs=True):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return {"ok": False, "error": "创作者不存在"}
        cancelled = 0
        if cancel_jobs:
            from content_factory.collector.store import CollectionJobStore
            cancelled = CollectionJobStore(self.store).cancel_for_creator(row["id"])
        self.monitor.delete_creator_bundle(row["id"])
        return {"ok": True, "removed": True, "cancelledJobs": cancelled, "handle": row["handle"]}

    def creator(self, creator_id):
        row = self._creator_or_error(creator_id)
        return self._decorate(row)

    def creator_by_handle(self, handle):
        row = self.monitor.find_by_handle(handle)
        return self.creator(row["id"]) if row else None

    def update_creator_fields(self, creator_id, **fields):
        """回写 Creator contract 里的展示字段（头像 / 粉丝数 / 作品数等）。"""
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return {"ok": False, "error": "创作者不存在"}
        changed = self.monitor.update_creator_row(row["id"], **fields)
        return {"ok": True, "changed": changed}

    def creators(self, enabled=None, search="", category="", status="", due_only=False,
                 limit=200, now=None):
        clauses, params = [], []
        if enabled is not None:
            clauses.append("COALESCE(s.enabled, 1)=?")
            params.append(1 if enabled else 0)
        if category:
            clauses.append("c.category=?")
            params.append(category)
        if status:
            clauses.append("c.status=?")
            params.append(status)
        if search:
            clauses.append("(c.handle LIKE ? OR c.display_name LIKE ? OR c.category LIKE ?)")
            needle = f"%{search}%"
            params.extend([needle, needle, needle])
        if due_only:
            clauses.append("COALESCE(s.enabled, 1)=1")
            clauses.append("(s.next_check_at IS NULL OR s.next_check_at='' OR s.next_check_at<=?)")
            params.append(self._stamp() if now is None else now)
        where = " AND ".join(clauses)
        rows = self.monitor.creators_with_state(where, params, limit=int(limit))
        return [self._decorate(row) for row in rows]

    # ---- enable / disable ----------------------------------------------
    def set_enabled(self, creator_id, enabled):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return {"ok": False, "error": "创作者不存在"}
        enabled = bool(enabled)
        state_fields = {"enabled": 1 if enabled else 0}
        if enabled:
            # 重新启用 = 用户想马上看到结果：把下次检查拉到当前时刻。
            state_fields["next_check_at"] = self._stamp()
        self.monitor.save_state_and_creator(row["id"], state_fields=state_fields,
                                            creator_fields={"status": "active" if enabled else "paused"})
        return {"ok": True, "enabled": enabled, "creator": self.creator(row["id"])}

    def enable(self, creator_id):
        return self.set_enabled(creator_id, True)

    def disable(self, creator_id):
        return self.set_enabled(creator_id, False)

    # ---- 节奏 / 优先级 --------------------------------------------------
    def set_poll_interval(self, creator_id, value):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return {"ok": False, "error": "创作者不存在"}
        seconds = intervals.parse_interval(value, self.default_interval_seconds())
        text = intervals.format_interval(seconds)
        self.monitor.save_state_and_creator(
            row["id"],
            state_fields={"poll_interval_seconds": seconds,
                          "next_check_at": self._next_after(row["id"], seconds)},
            creator_fields={"poll_interval": text})
        return {"ok": True, "pollIntervalSeconds": seconds, "pollInterval": text,
                "creator": self.creator(row["id"])}

    def set_priority(self, creator_id, value):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return {"ok": False, "error": "创作者不存在"}
        priority = intervals.normalize_priority(value)
        self.monitor.update_creator_row(row["id"], priority=priority)
        return {"ok": True, "priority": priority, "creator": self.creator(row["id"])}

    # ---- 检查状态 ------------------------------------------------------
    def state(self, creator_id):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return None
        return self.monitor.state(row["id"])

    def resolved_interval(self, creator_row):
        """Creator 的检查周期（秒）：显式设置优先，其次 creator.poll_interval 文本。"""
        state = self.monitor.state(creator_row["id"]) or {}
        try:
            explicit = int(state.get("poll_interval_seconds") or 0)
        except (TypeError, ValueError):
            explicit = 0
        if explicit > 0:
            return intervals.clamp_seconds(explicit)
        return intervals.parse_interval(creator_row.get("poll_interval"),
                                        self.default_interval_seconds())

    def _next_after(self, creator_id, seconds):
        return intervals.shift(self._stamp(), seconds)

    def due_creators(self, limit=50, now=None):
        stamp = self._stamp() if now is None else now
        rows = self.monitor.due(stamp, limit=limit)
        return [self._decorate(row) for row in rows]

    def backoff_seconds(self, failures, interval_seconds):
        """连续失败时指数退避：interval × 2^n，封顶 6 小时。

        第一次失败就退到两倍间隔（而不是原间隔）：失败通常意味着被限流 /
        被要求安全验证，紧接着再敲一遍只会让情况更糟。
        """
        failures = max(1, int(failures or 0))
        return min(MAX_BACKOFF_SECONDS, int(interval_seconds) * (2 ** min(failures, 4)))

    def mark_checked(self, creator_id, state="success", error="", discovered=0,
                     queued=0, next_check_at=None, message=""):
        """一次检查结束后落状态：上次检查时间 / 下次检查时间 / 失败计数。

        state: success（抓全）/ partial（抓了一部分）/ failed（失败）。
        """
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return {"ok": False, "error": "创作者不存在"}
        state = state if state in ("success", "partial", "failed") else "success"
        current = self.monitor.state(row["id"]) or {}
        interval = self.resolved_interval(row)
        failed = state == "failed"
        failures = (int(current.get("consecutive_failures") or 0) + 1) if failed else 0
        if next_check_at is None:
            wait = self.backoff_seconds(failures, interval) if failed else interval
            next_check_at = intervals.shift(self._stamp(), wait)
        self.monitor.save_state_and_creator(
            row["id"],
            state_fields={
                "last_check_at": self._stamp(),
                "next_check_at": next_check_at,
                "last_state": state,
                "last_error": str(error or "")[:1000],
                "last_discovered": int(discovered or 0),
                "last_queued": int(queued or 0),
                "consecutive_failures": failures,
                "total_checks": int(current.get("total_checks") or 0) + 1,
                "total_queued": int(current.get("total_queued") or 0) + int(queued or 0)},
            creator_fields={"status": "error" if failed else (
                "active" if current.get("enabled", 1) else "paused")})
        return {"ok": True, "state": state, "nextCheckAt": next_check_at,
                "consecutiveFailures": failures, "intervalSeconds": interval,
                "message": message}

    def schedule_next(self, creator_id, seconds=None, from_stamp=None):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return {"ok": False, "error": "创作者不存在"}
        wait = self.resolved_interval(row) if seconds is None else int(seconds)
        stamp = intervals.shift(from_stamp or self._stamp(), wait)
        self.monitor.state(row["id"])
        self.monitor.update_state(row["id"], next_check_at=stamp)
        return {"ok": True, "nextCheckAt": stamp}

    # ---- 抢占（重复采集保护）-------------------------------------------
    def begin_check(self, creator_id, owner="", lease_seconds=1800):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return False
        return self.monitor.acquire(row["id"], owner=owner, lease_seconds=lease_seconds)

    def end_check(self, creator_id, owner=""):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return False
        return bool(self.monitor.release(row["id"], owner=owner))

    def is_checking(self, creator_id):
        row = self.monitor.creator_row(str(creator_id or ""))
        if not row:
            return False
        state = self.monitor.state(row["id"]) or {}
        return bool(str(state.get("running_since") or "").strip())

    def expire_leases(self, lease_seconds=1800):
        return self.monitor.expire_leases(lease_seconds)

    # ---- 视图 ----------------------------------------------------------
    def stats(self, now=None):
        rows = self.monitor.creators_with_state(limit=2000)
        decorated = [self._decorate(row) for row in rows]
        errors = [row for row in decorated if row.get("last_state") == "failed"]
        return {
            "total": len(decorated),
            "enabled": sum(1 for row in decorated if row["enabled"]),
            "disabled": sum(1 for row in decorated if not row["enabled"]),
            "due": sum(1 for row in decorated if row["is_due"]),
            "checking": sum(1 for row in decorated if row["checking"]),
            "failing": len(errors),
            "itemCounts": self.monitor.item_counts_by_creator(),
        }


def _normalize_fields(fields):
    """驼峰别名 -> 服务层字段名，并回报认不出的键。

    回报而不是静默丢弃：界面上写错一个字段名却收到 ok=true，
    会变成「保存成功但什么都没改」，是最难排查的一类问题。
    """
    normalized, ignored = {}, []
    for key, value in (fields or {}).items():
        name = FIELD_ALIASES.get(key, key)
        if name in CREATOR_EDITABLE or name in ("handle", "enabled", "poll_interval_seconds"):
            normalized[name] = value
        elif key not in IGNORED_KEYS:
            ignored.append(key)
    return normalized, ignored
