"""Job 数据模型 + 任务表的 sqlite 持久化。

为什么需要这一层（而不是像原来那样把待办放在 `pipeline._queue` 列表里）：
- 那个列表是**进程内存**，程序一关就没了：用户排了 200 条，重启后全丢。
- 也没有「谁正在跑」的记录，所以重启后无法知道哪些是中途断掉的。
- 更没有并发保护：两次点击「批量分析」会给同一条内容排两个任务。

Job 的状态机（与既有内容状态机对齐，不另造一套）：

    pending ──▶ queued ──▶ running ──▶ done
                  ▲           │
                  │           ├── 还能重试 ──▶ retrying ──(退避到点)──▶ running
                  │           └── 用尽次数 ──▶ failed
                  └──── 恢复 / 手动重排 ────────┘
                              cancelled（人为中止，终态）

    pending  刚建、还没到可执行时间（例如带延迟入队 / 等依赖）
    queued   已就绪，可以被 worker 认领
    running  已被某个 worker 认领（有租约，见 lease_owner / lease_expires_at）
    retrying 失败后等退避时间，到点后重新可认领
    done / failed / cancelled  终态

**活跃状态**（pending/queued/running/retrying）由数据库的部分唯一索引兜底：
同一个 dedupe_key 只允许一条活跃任务 —— 这就是「避免同一 ContentItem 重复同时执行」
的硬保证，进程重启、多线程、甚至两个进程都绕不过去。

存储位置：与内容库同一个 sqlite 文件（默认 %LOCALAPPDATA%/TikTokBatchMVP/content-factory.db），
原因是「备份一个文件就够」。建表全部 IF NOT EXISTS，不动现有表。
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .settings_store import app_data_root

# ---- 状态 --------------------------------------------------------------
STATE_PENDING = "pending"
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_RETRYING = "retrying"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

ACTIVE_STATES = (STATE_PENDING, STATE_QUEUED, STATE_RUNNING, STATE_RETRYING)
TERMINAL_STATES = (STATE_DONE, STATE_FAILED, STATE_CANCELLED)
ALL_STATES = ACTIVE_STATES + TERMINAL_STATES

# 认领时「可以开跑」的状态：到点之后 queued / pending / retrying 一视同仁。
# 三个状态的区别只是「怎么走到这里的」，调度上不需要再分。
CLAIMABLE_STATES = (STATE_QUEUED, STATE_PENDING, STATE_RETRYING)

STATE_LABELS = {
    STATE_PENDING: "待处理",
    STATE_QUEUED: "排队中",
    STATE_RUNNING: "执行中",
    STATE_RETRYING: "等待重试",
    STATE_DONE: "已完成",
    STATE_FAILED: "失败",
    STATE_CANCELLED: "已取消",
}

# 阶段名（与 pipeline 的三个阶段一致；不在这里定义阶段语义，只做白名单）
JOB_KINDS = ("download", "transcript", "enrich")

JOB_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    seq               INTEGER,
    kind              TEXT NOT NULL,
    content_id        TEXT DEFAULT '',
    dedupe_key        TEXT NOT NULL,
    state             TEXT NOT NULL DEFAULT 'pending',
    priority          INTEGER DEFAULT 0,
    attempts          INTEGER DEFAULT 0,
    max_attempts      INTEGER DEFAULT 3,
    payload           TEXT DEFAULT '{}',
    result            TEXT DEFAULT '{}',
    error             TEXT DEFAULT '',
    available_at      REAL DEFAULT 0,
    lease_owner       TEXT DEFAULT '',
    lease_expires_at  REAL DEFAULT 0,
    run_token         TEXT DEFAULT '',
    created_at        TEXT DEFAULT '',
    updated_at        TEXT DEFAULT '',
    started_at        TEXT DEFAULT '',
    finished_at       TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state, available_at);
CREATE INDEX IF NOT EXISTS idx_jobs_kind ON jobs(kind, state);
CREATE INDEX IF NOT EXISTS idx_jobs_content ON jobs(content_id, kind);
CREATE INDEX IF NOT EXISTS idx_jobs_seq ON jobs(seq);
-- 「同一条内容同一个阶段只能有一个活跃任务」的硬约束（部分唯一索引）
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_dedupe
    ON jobs(dedupe_key) WHERE state IN ('pending', 'queued', 'running', 'retrying');
"""

# 允许被 update() 改的列（白名单，避免调用方写错列名或改主键）
MUTABLE_FIELDS = (
    "state", "priority", "attempts", "max_attempts", "payload", "result", "error",
    "available_at", "lease_owner", "lease_expires_at", "run_token", "started_at",
    "finished_at", "kind", "content_id", "dedupe_key",
)

JSON_FIELDS = ("payload", "result")


def new_job_id():
    return f"job_{uuid.uuid4().hex[:12]}"


def now_text(stamp=None):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp if stamp else time.time()))


def default_dedupe_key(kind, content_id):
    """默认去重键：同一内容的同一阶段只排一个。

    内容 id 为空（例如将来的「全库扫描」类任务）时退化成 kind 自身，
    这样同类全局任务也只允许一个 —— 比"随便重复排队"安全。
    """
    return f"{kind or 'job'}:{content_id or '-'}"


def content_lock_key(kind, content_id):
    """内容级并发锁的键（仅用于说明与日志，真正的判定在 SQL 里）。

    与 dedupe_key 的区别：dedupe_key 管「同一条内容的同一个阶段」，
    这个管「同一条内容」——认领任务时会跳过已有任务在跑的内容，
    所以不会出现「下载还没完就开始转写」这种并行。
    """
    return str(content_id or "")


class Job:
    """一条流水线任务。

    - 字段是稳定契约（见 docs/pipeline-core.md），新增字段必须给默认值；
    - `payload` / `result` 是自由 JSON，队列不解释它们的内容；
    - 同时支持属性访问（job.state）和少量字典访问（job.get("state")），
      这样它可以直接喂给界面层的 JSON 序列化。
    """

    __slots__ = ("id", "seq", "kind", "content_id", "dedupe_key", "state", "priority",
                 "attempts", "max_attempts", "payload", "result", "error", "available_at",
                 "lease_owner", "lease_expires_at", "run_token", "created_at", "updated_at",
                 "started_at", "finished_at")

    def __init__(self, id="", seq=0, kind="", content_id="", dedupe_key="", state=STATE_PENDING,
                 priority=0, attempts=0, max_attempts=3, payload=None, result=None, error="",
                 available_at=0.0, lease_owner="", lease_expires_at=0.0, run_token="",
                 created_at="", updated_at="", started_at="", finished_at=""):
        self.id = id
        self.seq = int(seq or 0)
        self.kind = kind
        self.content_id = content_id
        self.dedupe_key = dedupe_key or default_dedupe_key(kind, content_id)
        self.state = state
        self.priority = int(priority or 0)
        self.attempts = int(attempts or 0)
        self.max_attempts = max(1, int(max_attempts or 1))
        self.payload = payload if isinstance(payload, dict) else {}
        self.result = result if isinstance(result, dict) else {}
        self.error = str(error or "")
        self.available_at = float(available_at or 0.0)
        self.lease_owner = lease_owner or ""
        self.lease_expires_at = float(lease_expires_at or 0.0)
        self.run_token = run_token or ""
        self.created_at = created_at or ""
        self.updated_at = updated_at or ""
        self.started_at = started_at or ""
        self.finished_at = finished_at or ""

    # ---- 只读派生属性 --------------------------------------------------
    @property
    def is_active(self):
        return self.state in ACTIVE_STATES

    @property
    def is_terminal(self):
        return self.state in TERMINAL_STATES

    @property
    def state_label(self):
        return STATE_LABELS.get(self.state, self.state)

    @property
    def retries_left(self):
        return max(0, self.max_attempts - self.attempts)

    # ---- 序列化 --------------------------------------------------------
    def to_dict(self):
        return {
            "id": self.id,
            "seq": self.seq,
            "kind": self.kind,
            "contentId": self.content_id,
            "dedupeKey": self.dedupe_key,
            "state": self.state,
            "stateLabel": self.state_label,
            "priority": self.priority,
            "attempts": self.attempts,
            "maxAttempts": self.max_attempts,
            "retriesLeft": self.retries_left,
            "payload": dict(self.payload),
            "result": dict(self.result),
            "error": self.error,
            "availableAt": self.available_at,
            "leaseOwner": self.lease_owner,
            "leaseExpiresAt": self.lease_expires_at,
            "runToken": self.run_token,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
            "active": self.is_active,
            "terminal": self.is_terminal,
        }

    # 少量字典风格访问，方便直接扔给 JSON 边界
    def get(self, key, default=None):
        return self.to_dict().get(key, default)

    def __getitem__(self, key):
        return self.to_dict()[key]

    def __contains__(self, key):
        return key in self.to_dict()

    def copy(self, **changes):
        """派生一份副本（改几个字段），不改原对象。"""
        data = {name: getattr(self, name) for name in self.__slots__}
        data.update(changes)
        return Job(**data)

    def __repr__(self):
        return (f"<Job {self.id} {self.kind} content={self.content_id or '-'} "
                f"{self.state} {self.attempts}/{self.max_attempts}>")

    def __eq__(self, other):
        return isinstance(other, Job) and self.id == other.id and self.state == other.state

    def __hash__(self):
        return hash((self.id, self.state))

    # ---- 从数据库行还原 ------------------------------------------------
    @classmethod
    def from_row(cls, row):
        if row is None:
            return None
        data = dict(row)

        def decoded(field):
            raw = data.get(field)
            if isinstance(raw, (dict, list)):
                return raw
            try:
                value = json.loads(raw or "{}")
            except Exception:
                value = {}
            return value if isinstance(value, dict) else {}

        return cls(
            id=data.get("id") or "",
            seq=data.get("seq") or 0,
            kind=data.get("kind") or "",
            content_id=data.get("content_id") or "",
            dedupe_key=data.get("dedupe_key") or "",
            state=data.get("state") or STATE_PENDING,
            priority=data.get("priority") or 0,
            attempts=data.get("attempts") or 0,
            max_attempts=data.get("max_attempts") or 3,
            payload=decoded("payload"),
            result=decoded("result"),
            error=data.get("error") or "",
            available_at=data.get("available_at") or 0.0,
            lease_owner=data.get("lease_owner") or "",
            lease_expires_at=data.get("lease_expires_at") or 0.0,
            run_token=data.get("run_token") or "",
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
            started_at=data.get("started_at") or "",
            finished_at=data.get("finished_at") or "",
        )


def current_run_token():
    """本次进程运行的唯一标识。

    为什么不用 pid：同一次运行里可能创建多个队列实例（测试、多个 Api），
    而「恢复」要能区分「这次运行留下的」和「上次进程留下的」。
    形如 31415-a1b2c3：pid 便于人工排查，随机段保证唯一。
    """
    return f"{os.getpid()}-{uuid.uuid4().hex[:6]}"


class JobStore:
    """任务表的持久化层（只管存取，不管调度策略）。"""

    def __init__(self, path=None, timeout=20.0):
        self.path = Path(path) if path else app_data_root() / "content-factory.db"
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._ready = False
        self._shared = None

    # ---- 连接 / 事务 ---------------------------------------------------
    def connect(self):
        """新建一个独立连接（init 与测试用）。日常读写走 connection()。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=self.timeout,
                                     check_same_thread=False, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=20000")
        # 任务表是「高频小事务」（每一次入队 / 认领 / 完成都是一次提交）。
        # WAL + synchronous=NORMAL：应用崩溃不丢，掉电才可能丢最后几条 ——
        # 队列本身有恢复机制，这个取舍换来的是提交快一个数量级。
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    def connection(self):
        """进程内共享的长连接。

        为什么要共享：任务表每个操作都是一次小事务，而 sqlite 上
        「开连接 + 提交 + 关连接」的成本远高于语句本身（实测：慢盘上
        一次写入 1200ms，其中提交 460ms、关闭 570ms；共享连接后 20ms）。
        所有读写都已经被 self._lock 串行化，所以单个连接是安全的。
        """
        with self._lock:
            if self._shared is None:
                self._shared = self.connect()
            return self._shared

    def close(self):
        """关闭共享连接（测试清理、程序退出时调用）。"""
        with self._lock:
            if self._shared is not None:
                try:
                    self._shared.close()
                except Exception:
                    pass
                self._shared = None
        return True

    def init(self):
        with self._lock:
            if self._ready:
                return self
            connection = self.connection()
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(JOB_SCHEMA)
            self._ready = True
            return self

    @contextmanager
    def _tx(self):
        """写事务：进程内锁 + BEGIN IMMEDIATE（跨进程也安全）。

        为什么不用 `with connection:` 的隐式事务：认领任务必须「先选后改」，
        中间不能被别的 worker 插队，所以要显式 BEGIN IMMEDIATE 把读也锁住。
        """
        self.init()
        with self._lock:
            connection = self.connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except Exception:
                    pass
                raise

    def _rows(self, sql, params=()):
        self.init()
        with self._lock:
            return [dict(row) for row in self.connection().execute(sql, params).fetchall()]

    def _row(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # ---- 写入 ----------------------------------------------------------
    def insert(self, job):
        """插入一条任务。

        - seq 为空时在事务里取号（保证 FIFO 顺序不会因为并发入队而错乱）；
        - 活跃去重键冲突时抛 sqlite3.IntegrityError（由队列翻译成人话）。
        """
        payload = json.dumps(job.payload or {}, ensure_ascii=False)
        result = json.dumps(job.result or {}, ensure_ascii=False)
        stamp = now_text()
        with self._tx() as connection:
            seq = job.seq
            if not seq:
                row = connection.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM jobs").fetchone()
                seq = int(row["next"])
            connection.execute(
                "INSERT INTO jobs (id, seq, kind, content_id, dedupe_key, state, priority,"
                " attempts, max_attempts, payload, result, error, available_at, lease_owner,"
                " lease_expires_at, run_token, created_at, updated_at, started_at, finished_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job.id, seq, job.kind, job.content_id, job.dedupe_key, job.state,
                 job.priority, job.attempts, job.max_attempts, payload, result, job.error,
                 job.available_at, job.lease_owner, job.lease_expires_at, job.run_token,
                 job.created_at or stamp, job.updated_at or stamp,
                 job.started_at, job.finished_at))
        return self.get(job.id)

    def next_seq(self):
        row = self._row("SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM jobs")
        return int(row["next"]) if row else 1

    def update(self, job_id, **fields):
        payload = {key: value for key, value in fields.items() if key in MUTABLE_FIELDS}
        if not payload:
            return self.get(job_id)
        for field in JSON_FIELDS:
            if field in payload and not isinstance(payload[field], str):
                payload[field] = json.dumps(payload[field] or {}, ensure_ascii=False)
        payload["updated_at"] = now_text()
        clause = ", ".join(f"{key}=?" for key in payload)
        with self._tx() as connection:
            connection.execute(f"UPDATE jobs SET {clause} WHERE id=?",
                               (*payload.values(), job_id))
        return self.get(job_id)

    def delete(self, job_id):
        with self._tx() as connection:
            return connection.execute("DELETE FROM jobs WHERE id=?", (job_id,)).rowcount

    def clear(self, terminal_only=True):
        """清理任务表。terminal_only=True 只删终态任务（活跃任务不动）。"""
        with self._tx() as connection:
            if terminal_only:
                marks = ", ".join("?" for _ in TERMINAL_STATES)
                return connection.execute(f"DELETE FROM jobs WHERE state IN ({marks})",
                                          TERMINAL_STATES).rowcount
            return connection.execute("DELETE FROM jobs").rowcount

    # ---- 读取 ----------------------------------------------------------
    def get(self, job_id):
        return Job.from_row(self._row("SELECT * FROM jobs WHERE id=?", (job_id,)))

    def find_active(self, dedupe_key):
        marks = ", ".join("?" for _ in ACTIVE_STATES)
        return Job.from_row(self._row(
            f"SELECT * FROM jobs WHERE dedupe_key=? AND state IN ({marks})"
            " ORDER BY seq DESC LIMIT 1", (dedupe_key, *ACTIVE_STATES)))

    def find_active_for_content(self, content_id, kind=None):
        if not content_id:
            return None
        marks = ", ".join("?" for _ in ACTIVE_STATES)
        sql = f"SELECT * FROM jobs WHERE content_id=? AND state IN ({marks})"
        params = [str(content_id), *ACTIVE_STATES]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        sql += " ORDER BY seq ASC LIMIT 1"
        return Job.from_row(self._row(sql, tuple(params)))

    def active_content_ids(self):
        """当前有活跃任务的内容 id 集合（界面用来标「排队中」）。"""
        marks = ", ".join("?" for _ in ACTIVE_STATES)
        rows = self._rows(f"SELECT DISTINCT content_id FROM jobs WHERE state IN ({marks})"
                          f" AND content_id<>''", ACTIVE_STATES)
        return {row["content_id"] for row in rows}

    def jobs(self, state=None, states=None, kind=None, content_id=None, limit=200,
             order="seq ASC"):
        sql = "SELECT * FROM jobs"
        clauses, params = [], []
        states = tuple(states) if states else ((state,) if state else ())
        if states:
            marks = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({marks})")
            params.extend(states)
        if kind:
            clauses.append("kind=?")
            params.append(kind)
        if content_id:
            clauses.append("content_id=?")
            params.append(str(content_id))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {order} LIMIT {int(limit)}"
        return [Job.from_row(row) for row in self._rows(sql, tuple(params))]

    def counts(self):
        """按状态计数（界面 / 测试用）。"""
        counts = {state: 0 for state in ALL_STATES}
        for row in self._rows("SELECT state, COUNT(*) AS total FROM jobs GROUP BY state"):
            counts[row["state"]] = int(row["total"])
        counts["total"] = sum(counts[state] for state in ALL_STATES)
        counts["active"] = sum(counts[state] for state in ACTIVE_STATES)
        return counts

    # ---- 认领（调度核心） ----------------------------------------------
    def _claimable_sql(self, now, kinds=None, respect_content_lock=True):
        """「下一条该跑谁」的查询：优先级高的先跑，同优先级按 seq（= FIFO）。"""
        marks = ", ".join("?" for _ in CLAIMABLE_STATES)
        sql = [f"SELECT j.* FROM jobs AS j WHERE j.state IN ({marks}) AND j.available_at <= ?"]
        params = [*CLAIMABLE_STATES, float(now)]
        if kinds:
            marks = ", ".join("?" for _ in kinds)
            sql.append(f"AND j.kind IN ({marks})")
            params.extend(kinds)
        if respect_content_lock:
            sql.append("AND NOT EXISTS (SELECT 1 FROM jobs AS o WHERE o.id <> j.id"
                       " AND o.state = 'running' AND j.content_id <> ''"
                       " AND o.content_id = j.content_id)")
        sql.append("ORDER BY j.priority DESC, j.seq ASC LIMIT 1")
        return " ".join(sql), params

    def next_claimable(self, kinds=None, now=None, respect_content_lock=True):
        """查询「下一条会被认领的任务」，只读，不改状态。"""
        now = float(now if now is not None else time.time())
        sql, params = self._claimable_sql(now, kinds=kinds,
                                          respect_content_lock=respect_content_lock)
        return Job.from_row(self._row(sql, tuple(params)))

    def claim(self, worker_id="", run_token="", kinds=None, lease_seconds=900.0,
              now=None, respect_content_lock=True):
        """原子地认领一条可执行任务，并把它置为 running。

        respect_content_lock：同一条内容已经有任务在跑时跳过，
        保证「同一 ContentItem 不会同时执行两个阶段」。
        """
        now = float(now if now is not None else time.time())
        sql, params = self._claimable_sql(now, kinds=kinds,
                                          respect_content_lock=respect_content_lock)
        marks = ", ".join("?" for _ in CLAIMABLE_STATES)
        with self._tx() as connection:
            row = connection.execute(sql, tuple(params)).fetchone()
            if row is None:
                return None
            job_id = row["id"]
            updated = connection.execute(
                "UPDATE jobs SET state=?, attempts=attempts+1, lease_owner=?, lease_expires_at=?,"
                " run_token=?, started_at=?, updated_at=?, error=''"
                f" WHERE id=? AND state IN ({marks})",
                (STATE_RUNNING, worker_id or "", now + float(lease_seconds), run_token or "",
                 now_text(now), now_text(now), job_id, *CLAIMABLE_STATES)).rowcount
            if updated != 1:                   # 极小概率被别的进程抢先，当作没认领到
                return None
            fresh = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.from_row(fresh)

    def heartbeat(self, job_id, lease_seconds=900.0, now=None):
        now = float(now if now is not None else time.time())
        return self.update(job_id, lease_expires_at=now + float(lease_seconds))

    def stale_running(self, now=None, run_token=None):
        """找出「已经没人管」的 running 任务。

        两种来源：
        1. run_token 不是本次运行 -> 上次进程被关掉/崩掉时留下的；
        2. 租约过期 -> worker 卡死或被杀，同一次运行内也要能救回来。
        """
        now = float(now if now is not None else time.time())
        rows = self._rows("SELECT * FROM jobs WHERE state=?", (STATE_RUNNING,))
        stale = []
        for row in rows:
            job = Job.from_row(row)
            other_run = bool(run_token) and job.run_token != run_token
            expired = job.lease_expires_at <= now
            if other_run or expired:
                stale.append(job)
        return stale
