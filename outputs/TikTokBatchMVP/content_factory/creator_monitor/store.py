"""Creator 监控状态的持久化：sqlite（与内容库同一个库文件）。

为什么要单独一张表，而不是往 creators 上加字段：
- 现有 Creator contract（id / handle / display_name / avatar / category / status /
  followers / videos / priority / poll_interval / created_at / updated_at）是已经
  被界面、mock 数据和别的模块依赖的契约，**不能动**。
- 「有没有在跑」「上次检查是什么时间」「下次该什么时候查」是**监控器自己的状态**，
  不是 Creator 的属性；放在 creator_monitor_state 里，一对一挂在 creator_id 上。
- 老数据不用迁移：LEFT JOIN + COALESCE 让没有状态行的 Creator 默认「启用、立刻可查」，
  所以升级上来时已存在的创作者马上就能被纳入监控。

写入策略：所有落盘都必须走 write_all()（一个事务里提交多条语句）。
这台机器上一次 sqlite 提交（WAL fsync）是秒级的，而「建 Creator」「记录一次检查结果」
本来就是多张表要一起改 —— 拆成多次提交既慢又可能只改一半。读操作一律先读后建，
不会为了拿一行状态就产生一次写。
"""

import threading
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS creator_monitor_state (
    creator_id            TEXT PRIMARY KEY,
    enabled               INTEGER DEFAULT 1,
    poll_interval_seconds INTEGER DEFAULT 0,
    last_check_at         TEXT DEFAULT '',
    next_check_at         TEXT DEFAULT '',
    last_state            TEXT DEFAULT '',
    last_error            TEXT DEFAULT '',
    last_discovered       INTEGER DEFAULT 0,
    last_queued           INTEGER DEFAULT 0,
    consecutive_failures  INTEGER DEFAULT 0,
    total_checks          INTEGER DEFAULT 0,
    total_queued          INTEGER DEFAULT 0,
    running_since         TEXT DEFAULT '',
    run_owner             TEXT DEFAULT '',
    created_at            TEXT DEFAULT '',
    updated_at            TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_monitor_due
    ON creator_monitor_state(enabled, next_check_at);
"""

# 允许通过本模块回写的 Creator contract 列（不含 id / handle / created_at）
CREATOR_FIELDS = ("display_name", "avatar", "category", "status", "followers",
                  "videos", "priority", "poll_interval")

STATE_FIELDS = ("enabled", "poll_interval_seconds", "last_check_at", "next_check_at",
                "last_state", "last_error", "last_discovered", "last_queued",
                "consecutive_failures", "total_checks", "total_queued",
                "running_since", "run_owner")

# 高 -> 中 -> 低，然后按「最早该检查」排
DUE_ORDER = ("CASE c.priority WHEN '高' THEN 0 WHEN '中' THEN 1 WHEN '低' THEN 2 ELSE 1 END, "
             "COALESCE(NULLIF(s.next_check_at,''), '') ASC, c.updated_at DESC, c.created_at DESC")

SELECT_WITH_STATE = """
SELECT c.*,
       COALESCE(s.enabled, 1)                     AS enabled,
       COALESCE(s.poll_interval_seconds, 0)       AS poll_interval_seconds,
       COALESCE(s.last_check_at, '')              AS last_check_at,
       COALESCE(s.next_check_at, '')              AS next_check_at,
       COALESCE(s.last_state, '')                 AS last_state,
       COALESCE(s.last_error, '')                 AS last_error,
       COALESCE(s.last_discovered, 0)             AS last_discovered,
       COALESCE(s.last_queued, 0)                 AS last_queued,
       COALESCE(s.consecutive_failures, 0)        AS consecutive_failures,
       COALESCE(s.total_checks, 0)                AS total_checks,
       COALESCE(s.total_queued, 0)                AS total_queued,
       COALESCE(s.running_since, '')              AS running_since,
       COALESCE(s.run_owner, '')                  AS run_owner
FROM creators c
LEFT JOIN creator_monitor_state s ON s.creator_id = c.id
"""


class CreatorMonitorStore:
    """creator_monitor_state 的读写 + creators 契约列的受限回写。"""

    def __init__(self, store, now=None):
        self.base = store                      # FactoryStore：连接 / 建表 / 内容库都走它
        self._now = now                        # 可注入时钟（测试用假时钟，生产用挂钟）
        self._lock = threading.RLock()
        self._ready = False

    # ---- 基础 ----------------------------------------------------------
    def init(self):
        with self._lock:
            if self._ready:
                return self
            self.base.init()
            with closing(self.base.connect()) as connection, connection:
                connection.executescript(SCHEMA)
            self._ready = True
            return self

    def _rows(self, sql, params=()):
        self.init()
        with self._lock, closing(self.base.connect()) as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def _row(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def write_all(self, statements):
        """在一个事务里执行多条写语句，返回影响的行的总数。"""
        statements = [(sql, tuple(params)) for sql, params in statements if sql]
        if not statements:
            return 0
        self.init()
        changed = 0
        with self._lock, closing(self.base.connect()) as connection, connection:
            for sql, params in statements:
                changed += connection.execute(sql, params).rowcount
        return changed

    def _write(self, sql, params=()):
        return self.write_all([(sql, params)])

    # ---- 状态行 --------------------------------------------------------
    def state(self, creator_id, create=True):
        """读状态行；没有才建（先读后建，避免每次读取都产生一次写）。"""
        creator_id = str(creator_id or "")
        if not creator_id:
            return None
        row = self._row("SELECT * FROM creator_monitor_state WHERE creator_id=?", (creator_id,))
        if row is not None or not create:
            return row
        stamp = self._stamp()
        self._write("INSERT INTO creator_monitor_state (creator_id, created_at, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(creator_id) DO NOTHING",
                    (creator_id, stamp, stamp))
        return self._row("SELECT * FROM creator_monitor_state WHERE creator_id=?", (creator_id,))

    def update_state(self, creator_id, **fields):
        payload = self._state_payload(fields)
        if not payload:
            return 0
        clause = ", ".join(f"{key}=?" for key in payload)
        return self._write(f"UPDATE creator_monitor_state SET {clause} WHERE creator_id=?",
                           (*payload.values(), str(creator_id or "")))

    # ---- creators contract 列的受限回写 ---------------------------------
    def update_creator_row(self, creator_id, **fields):
        payload = self._creator_payload(fields)
        if not payload:
            return 0
        clause = ", ".join(f"{key}=?" for key in payload)
        return self._write(f"UPDATE creators SET {clause} WHERE id=?",
                           (*payload.values(), str(creator_id or "")))

    def update_handle(self, creator_id, handle):
        return self._write("UPDATE creators SET handle=?, updated_at=? WHERE id=?",
                           (handle, self._stamp(), str(creator_id or "")))

    def find_by_handle(self, handle):
        """handle 唯一性用「忽略大小写」判断：@Emily 与 @emily 是同一个人。"""
        text = str(handle or "").strip().lstrip("@")
        if not text:
            return None
        return self._row("SELECT * FROM creators WHERE LOWER(handle)=LOWER(?)", (text,))

    def creator_row(self, creator_id):
        return self._row("SELECT * FROM creators WHERE id=?", (str(creator_id or ""),))

    # ---- 组合写：一次事务改多张表 ---------------------------------------
    def create_creator_with_state(self, creator_id, handle, creator_fields=None,
                                  state_fields=None):
        """建 Creator + 建监控状态：一个事务，不会出现「有 Creator 没状态」的中间态。"""
        fields = creator_fields or {}
        state = dict(state_fields or {})
        stamp = self._stamp()
        state.setdefault("created_at", stamp)
        state.setdefault("updated_at", stamp)
        state_columns = list(state)
        statements = [(
            "INSERT INTO creators (id,handle,display_name,avatar,category,status,followers,"
            "videos,priority,poll_interval,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (creator_id, handle, fields.get("display_name") or handle,
             fields.get("avatar") or "", fields.get("category") or "",
             fields.get("status") or "active", _as_int(fields.get("followers")),
             _as_int(fields.get("videos")), fields.get("priority") or "中",
             fields.get("poll_interval") or "1 小时", stamp, stamp))]
        statements.append((
            "INSERT INTO creator_monitor_state (creator_id, " + ", ".join(state_columns)
            + ") VALUES (?, " + ", ".join("?" for _ in state_columns)
            + ") ON CONFLICT(creator_id) DO NOTHING",
            (creator_id, *state.values())))
        return self.write_all(statements)

    def save_state_and_creator(self, creator_id, state_fields=None, creator_fields=None):
        """一次提交里同时改监控状态与 Creator 展示字段（记录检查结果就是这种）。"""
        statements = []
        state = self._state_payload(state_fields or {})
        if state:
            clause = ", ".join(f"{key}=?" for key in state)
            statements.append((f"UPDATE creator_monitor_state SET {clause} WHERE creator_id=?",
                               (*state.values(), str(creator_id or ""))))
        creator = self._creator_payload(creator_fields or {})
        if creator:
            clause = ", ".join(f"{key}=?" for key in creator)
            statements.append((f"UPDATE creators SET {clause} WHERE id=?",
                               (*creator.values(), str(creator_id or ""))))
        return self.write_all(statements)

    def delete_creator_bundle(self, creator_id):
        """删 Creator + 删监控状态：一次事务（历史任务与记录保留作审计）。"""
        creator_id = str(creator_id or "")
        return self.write_all([
            ("DELETE FROM creator_monitor_state WHERE creator_id=?", (creator_id,)),
            ("DELETE FROM creators WHERE id=?", (creator_id,)),
        ])

    def _state_payload(self, fields):
        payload = {key: value for key, value in (fields or {}).items() if key in STATE_FIELDS}
        if payload:
            payload["updated_at"] = self._stamp()
        return payload

    def _creator_payload(self, fields):
        payload = {key: value for key, value in (fields or {}).items() if key in CREATOR_FIELDS}
        if payload:
            payload["updated_at"] = self._stamp()
        return payload

    # ---- 查询 ----------------------------------------------------------
    def creators_with_state(self, where="", params=(), order="c.updated_at DESC, c.created_at DESC",
                            limit=200):
        sql = SELECT_WITH_STATE
        if where:
            sql += f" WHERE {where}"
        sql += f" ORDER BY {order} LIMIT {int(limit)}"
        return self._rows(sql, tuple(params))

    def due(self, now_stamp, limit=50):
        sql = (SELECT_WITH_STATE
               + " WHERE COALESCE(s.enabled, 1)=1"
                 " AND (s.next_check_at IS NULL OR s.next_check_at='' OR s.next_check_at<=?)"
               + f" ORDER BY {DUE_ORDER} LIMIT {int(limit)}")
        return self._rows(sql, (now_stamp,))

    def item_counts_by_creator(self):
        rows = self._rows("SELECT creator_id, COUNT(*) AS total FROM content_items "
                          "WHERE creator_id<>'' GROUP BY creator_id")
        return {row["creator_id"]: row["total"] for row in rows}

    # ---- 抢占（重复采集保护）-------------------------------------------
    def acquire(self, creator_id, owner="", lease_seconds=1800):
        """带租约的 CAS 抢占：只有拿到的人才能真正去抓。

        running_since 既是「有别人正在跑」的标志，也是「跑了多久」的记录 ——
        进程崩溃留下的死租约到点自动失效，不需要人工插手。
        """
        creator_id = str(creator_id or "")
        self.state(creator_id)
        cutoff = _shift(self._stamp(), -int(lease_seconds))
        stamp = self._stamp()
        return self._write(
            "UPDATE creator_monitor_state SET running_since=?, run_owner=?, updated_at=? "
            "WHERE creator_id=? AND (running_since IS NULL OR running_since='' OR running_since<?)",
            (stamp, owner or "", stamp, creator_id, cutoff)) == 1

    def release(self, creator_id, owner=""):
        creator_id = str(creator_id or "")
        self.state(creator_id)
        stamp = self._stamp()
        if owner:
            return self._write(
                "UPDATE creator_monitor_state SET running_since='', run_owner='', updated_at=? "
                "WHERE creator_id=? AND run_owner=?", (stamp, creator_id, owner))
        return self._write(
            "UPDATE creator_monitor_state SET running_since='', run_owner='', updated_at=? "
            "WHERE creator_id=?", (stamp, creator_id))

    def expire_leases(self, lease_seconds=1800):
        """启动时清掉上一轮进程留下的死租约。"""
        cutoff = _shift(self._stamp(), -int(lease_seconds))
        return self._write(
            "UPDATE creator_monitor_state SET running_since='', run_owner='' "
            "WHERE running_since IS NOT NULL AND running_since<>'' AND running_since<?",
            (cutoff,))

    def _stamp(self):
        from .intervals import stamp
        return stamp(self._now() if self._now else None)


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _shift(stamp, seconds):
    from .intervals import shift
    return shift(stamp, seconds)
