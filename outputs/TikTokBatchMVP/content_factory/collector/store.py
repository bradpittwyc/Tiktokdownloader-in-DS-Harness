"""采集任务与采集记录的持久化：collection_jobs / collection_runs。

两条硬约束都在表结构里，而不是靠调用方自觉：

1. **同一个内容不会有两个未完成的 job**
   `CREATE UNIQUE INDEX ... ON collection_jobs(content_key) WHERE state IN
   ('pending','running')` —— 部分唯一索引。反复 polling、两个人同时点
   「立即检查」、进程重启后重放，都不会给同一个作品排出两个任务；
   而失败过的 job 不占用这个唯一位，所以重试仍然能建新任务。

2. **成功 / 失败都要留痕**
   每次检查写一条 collection_runs（发现多少、排队多少、跳过多少、为什么失败），
   界面上的「采集记录 / 失败记录」直接读它，不需要再去日志里翻。

job 的状态机：pending → running → done / failed；另有 skipped（策略跳过）与
cancelled（创作者被删除等）。Collector 只负责建任务，真正的下载由下载器执行，
执行方通过 mark_running / mark_done / mark_failed 回写，或者由
ContentCollector.reconcile() 依据内容库的实际结果自动收敛。
"""

import threading
from contextlib import closing

from content_factory.factory_store import new_id, now_text

JOB_FIELDS = (
    "batch_id", "creator_id", "creator_handle", "source_type", "source_video_id",
    "source_url", "content_key", "title", "description", "cover", "duration", "kind",
    "upload_date", "priority", "state", "attempts", "max_attempts", "content_item_id",
    "last_error", "scheduled_at", "finished_at",
)

RUN_FIELDS = (
    "batch_id", "creator_id", "creator_handle", "trigger", "state", "started_at",
    "finished_at", "discovered", "fresh", "queued", "duplicates", "filtered",
    "existing", "error", "message", "warning", "needs_verification", "complete",
)

ACTIVE_STATES = ("pending", "running")

# 高 -> 中 -> 低，然后先进先出
JOB_ORDER = ("CASE priority WHEN '高' THEN 0 WHEN '中' THEN 1 WHEN '低' THEN 2 ELSE 1 END, "
             "created_at ASC, rowid ASC")

SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_jobs (
    id               TEXT PRIMARY KEY,
    batch_id         TEXT DEFAULT '',
    creator_id       TEXT DEFAULT '',
    creator_handle   TEXT DEFAULT '',
    source_type      TEXT DEFAULT 'tiktok',
    source_video_id  TEXT DEFAULT '',
    source_url       TEXT DEFAULT '',
    content_key      TEXT DEFAULT '',
    title            TEXT DEFAULT '',
    description      TEXT DEFAULT '',
    cover            TEXT DEFAULT '',
    duration         INTEGER DEFAULT 0,
    kind             TEXT DEFAULT 'video',
    upload_date      TEXT DEFAULT '',
    priority         TEXT DEFAULT '中',
    state            TEXT DEFAULT 'pending',
    attempts         INTEGER DEFAULT 0,
    max_attempts     INTEGER DEFAULT 3,
    content_item_id  TEXT DEFAULT '',
    last_error       TEXT DEFAULT '',
    created_at       TEXT DEFAULT '',
    updated_at       TEXT DEFAULT '',
    scheduled_at     TEXT DEFAULT '',
    finished_at      TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_collection_jobs_active
    ON collection_jobs(content_key) WHERE state IN ('pending','running');
CREATE INDEX IF NOT EXISTS idx_collection_jobs_state
    ON collection_jobs(state, created_at);
CREATE INDEX IF NOT EXISTS idx_collection_jobs_creator
    ON collection_jobs(creator_id, created_at DESC);

CREATE TABLE IF NOT EXISTS collection_runs (
    id                 TEXT PRIMARY KEY,
    batch_id           TEXT DEFAULT '',
    creator_id         TEXT DEFAULT '',
    creator_handle     TEXT DEFAULT '',
    trigger            TEXT DEFAULT 'poll',
    state              TEXT DEFAULT 'success',
    started_at         TEXT DEFAULT '',
    finished_at        TEXT DEFAULT '',
    discovered         INTEGER DEFAULT 0,
    fresh              INTEGER DEFAULT 0,
    queued             INTEGER DEFAULT 0,
    duplicates         INTEGER DEFAULT 0,
    filtered           INTEGER DEFAULT 0,
    existing           INTEGER DEFAULT 0,
    error              TEXT DEFAULT '',
    message            TEXT DEFAULT '',
    warning            TEXT DEFAULT '',
    needs_verification INTEGER DEFAULT 0,
    complete           INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_collection_runs_creator
    ON collection_runs(creator_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_collection_runs_state
    ON collection_runs(state, started_at DESC);
"""


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


class CollectionJobStore:
    def __init__(self, store):
        self.base = store
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

    def _write(self, sql, params=()):
        self.init()
        with self._lock, closing(self.base.connect()) as connection, connection:
            return connection.execute(sql, params).rowcount

    # ---- job 写入 ------------------------------------------------------
    def enqueue(self, job):
        """建一个任务；同一个 content_key 已有未完成任务时返回既有任务，不新建。"""
        self.init()
        payload = {key: value for key, value in (job or {}).items() if key in JOB_FIELDS}
        content_key = str(payload.get("content_key") or "").strip()
        if not content_key:
            return {"created": False, "duplicate": False, "error": "缺少 content_key", "job": None}
        existing = self._row("SELECT * FROM collection_jobs WHERE content_key=? "
                             "AND state IN ('pending','running')", (content_key,))
        if existing:
            return {"created": False, "duplicate": True, "job": existing}

        job_id = str((job or {}).get("id") or new_id("cjob"))
        stamp = now_text()
        payload.setdefault("state", "pending")
        payload.setdefault("scheduled_at", stamp)
        columns = list(payload)
        with self._lock, closing(self.base.connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO collection_jobs (id, " + ", ".join(columns) + ", created_at, updated_at) "
                "VALUES (?, " + ", ".join("?" for _ in columns) + ", ?, ?) "
                "ON CONFLICT(content_key) WHERE state IN ('pending','running') DO NOTHING",
                (job_id, *payload.values(), stamp, stamp))
            created = cursor.rowcount == 1
        if not created:
            # 并发下被别人抢先建了同一个任务：把既有任务还回去，仍然算重复。
            return {"created": False, "duplicate": True,
                    "job": self._row("SELECT * FROM collection_jobs WHERE content_key=? "
                                     "AND state IN ('pending','running')", (content_key,))}
        return {"created": True, "duplicate": False, "job": self.job(job_id)}

    def enqueue_many(self, jobs):
        created, duplicates = [], []
        for job in jobs or []:
            result = self.enqueue(job)
            if result.get("error"):
                continue
            (created if result["created"] else duplicates).append(result["job"])
        return {"created": created, "duplicates": duplicates,
                "createdCount": len(created), "duplicateCount": len(duplicates),
                "jobs": created}

    # ---- job 读取 ------------------------------------------------------
    def job(self, job_id):
        return self._row("SELECT * FROM collection_jobs WHERE id=?", (str(job_id or ""),))

    def jobs(self, state=None, states=None, creator_id=None, batch_id=None, limit=100):
        clauses, params = [], []
        if states:
            marks = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({marks})")
            params.extend(list(states))
        elif state and state not in ("all", "全部", ""):
            clauses.append("state=?")
            params.append(state)
        if creator_id:
            clauses.append("creator_id=?")
            params.append(creator_id)
        if batch_id:
            clauses.append("batch_id=?")
            params.append(batch_id)
        sql = "SELECT * FROM collection_jobs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {JOB_ORDER} LIMIT {int(limit)}"
        return self._rows(sql, tuple(params))

    def pending(self, limit=20, creator_id=None):
        return self.jobs(states=("pending",), creator_id=creator_id, limit=limit)

    def active_keys(self, creator_id=None):
        rows = self.jobs(states=ACTIVE_STATES, creator_id=creator_id, limit=20000)
        return {row["content_key"] for row in rows if row["content_key"]}

    def history(self, creator_id=None, limit=20000):
        """content_key -> 最近一次任务（用于判断「要不要重试」）。"""
        rows = self.jobs(creator_id=creator_id, limit=limit)
        rows.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")),
                  reverse=True)
        latest = {}
        for row in rows:
            key = row.get("content_key") or ""
            if key and key not in latest:
                latest[key] = row
        return latest

    def counts(self, creator_id=None):
        sql = "SELECT state, COUNT(*) AS total FROM collection_jobs"
        params = ()
        if creator_id:
            sql += " WHERE creator_id=?"
            params = (creator_id,)
        sql += " GROUP BY state"
        counts = {row["state"] or "pending": row["total"] for row in self._rows(sql, params)}
        return {
            "total": sum(counts.values()),
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "done": counts.get("done", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "cancelled": counts.get("cancelled", 0),
            "byState": counts,
        }

    # ---- job 状态流转 --------------------------------------------------
    def _transition(self, job_id, state, **fields):
        payload = {key: value for key, value in fields.items()
                   if key in ("content_item_id", "last_error", "finished_at", "attempts",
                              "max_attempts", "priority", "scheduled_at")}
        payload["state"] = state
        payload["updated_at"] = now_text()
        if state in ("done", "failed", "skipped", "cancelled") and "finished_at" not in payload:
            payload["finished_at"] = payload["updated_at"]
        clause = ", ".join(f"{key}=?" for key in payload)
        return self._write(f"UPDATE collection_jobs SET {clause} WHERE id=?",
                           (*payload.values(), str(job_id or "")))

    def mark_running(self, job_id, attempts=None):
        fields = {"attempts": _as_int(attempts)} if attempts is not None else {}
        return self._transition(job_id, "running", **fields)

    def mark_done(self, job_id, content_item_id="", error=""):
        return self._transition(job_id, "done", content_item_id=content_item_id, last_error=error)

    def mark_failed(self, job_id, error=""):
        return self._transition(job_id, "failed", last_error=str(error or "")[:1000])

    def mark_skipped(self, job_id, reason=""):
        return self._transition(job_id, "skipped", last_error=str(reason or "")[:1000])

    def cancel(self, job_id, reason="任务已取消"):
        return self._transition(job_id, "cancelled", last_error=reason)

    def cancel_for_creator(self, creator_id):
        return self._write(
            "UPDATE collection_jobs SET state='cancelled', last_error='创作者已删除', "
            "finished_at=?, updated_at=? WHERE creator_id=? AND state IN ('pending','running')",
            (now_text(), now_text(), str(creator_id or "")))

    def requeue_stale(self, seconds=3600):
        """把卡在 running 太久的任务退回 pending（进程被杀后能自愈）。"""
        from content_factory.creator_monitor.intervals import shift
        cutoff = shift(now_text(), -int(seconds))
        return self._write(
            "UPDATE collection_jobs SET state='pending', last_error='上次执行超时，已重新排队', "
            "updated_at=? WHERE state='running' AND updated_at<?", (now_text(), cutoff))

    def failed_jobs(self, limit=50):
        return self.jobs(state="failed", limit=limit)

    # ---- run 记录 ------------------------------------------------------
    def record_run(self, creator_id="", creator_handle="", trigger="poll", state="success",
                   started_at="", finished_at="", discovered=0, fresh=0, queued=0,
                   duplicates=0, filtered=0, existing=0, error="", message="", warning="",
                   needs_verification=False, complete=False, batch_id="", run_id=None):
        run_id = run_id or new_id("crun")
        stamp = now_text()
        self._write(
            "INSERT INTO collection_runs (id, batch_id, creator_id, creator_handle, trigger, "
            "state, started_at, finished_at, discovered, fresh, queued, duplicates, filtered, "
            "existing, error, message, warning, needs_verification, complete) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, batch_id, str(creator_id or ""), str(creator_handle or ""), trigger, state,
             started_at or stamp, finished_at or stamp, _as_int(discovered), _as_int(fresh),
             _as_int(queued), _as_int(duplicates), _as_int(filtered), _as_int(existing),
             str(error or "")[:1000], str(message or "")[:1000], str(warning or "")[:1000],
             1 if needs_verification else 0, 1 if complete else 0))
        return self.run(run_id)

    def run(self, run_id):
        return self._row("SELECT * FROM collection_runs WHERE id=?", (str(run_id or ""),))

    def runs(self, creator_id=None, state=None, limit=100):
        clauses, params = [], []
        if creator_id:
            clauses.append("creator_id=?")
            params.append(creator_id)
        if state and state not in ("all", "全部", ""):
            clauses.append("state=?")
            params.append(state)
        sql = "SELECT * FROM collection_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY started_at DESC, rowid DESC LIMIT {int(limit)}"
        return self._rows(sql, tuple(params))

    def latest_run(self, creator_id):
        rows = self.runs(creator_id=creator_id, limit=1)
        return rows[0] if rows else None

    def failures(self, limit=50):
        return self.runs(state="failed", limit=limit)

    def run_counts(self, limit=2000):
        rows = self.runs(limit=limit)
        summary = {"total": len(rows), "success": 0, "partial": 0, "failed": 0,
                   "busy": 0, "skipped": 0}
        for row in rows:
            summary[row.get("state") or "success"] = summary.get(row.get("state") or "success", 0) + 1
        return summary

    def stats(self, limit=20):
        return {"jobs": self.counts(), "runs": self.run_counts(),
                "recentRuns": self.runs(limit=limit),
                "recentFailures": self.failures(limit=limit)}

    # ---- 内容库视图（只读，用于判断「这条内容是不是已经有了」）----------
    def library_index(self, creator_id="", creator_handle="", limit=20000):
        """content_key -> 内容库里那一条（只取判断需要的列，不做 N+1 装饰）。

        直接查 content_items 而不是走 FactoryStore.items()：后者会给每一行
        再查一次 AI 标注结果，一个 400 条内容的主页要几百次查询，
        轮询场景下没必要。这里只读、不改任何数据。
        """
        from .dedupe import key_for
        clauses, params = [], []
        if creator_id and creator_handle:
            clauses.append("(creator_id=? OR creator_handle=?)")
            params.extend([str(creator_id), str(creator_handle).lstrip("@")])
        elif creator_id:
            clauses.append("creator_id=?")
            params.append(str(creator_id))
        elif creator_handle:
            clauses.append("creator_handle=?")
            params.append(str(creator_handle).lstrip("@"))
        sql = ("SELECT id, source_type, source_video_id, source_url, creator_id, "
               "creator_handle, title, download_status, last_error, updated_at "
               "FROM content_items")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY COALESCE(NULLIF(updated_at,''), created_at) DESC LIMIT {int(limit)}"
        index = {}
        for row in self._rows(sql, tuple(params)):
            key = key_for(row.get("source_type"), row.get("source_video_id"), row.get("source_url"))
            if key and key not in index:
                index[key] = row
        return index

    def library_item(self, content_key):
        """按 content_key 精确取一条内容库记录（不依赖全表扫描）。"""
        from .dedupe import key_for, normalize_url
        text = str(content_key or "")
        if ":" not in text:
            return None
        source_type, ident = text.split(":", 1)
        columns = ("id, source_type, source_video_id, source_url, creator_id, creator_handle, "
                   "title, download_status, last_error")
        if ident.startswith("url:"):
            needle = normalize_url(ident[4:])
            rows = self._rows(f"SELECT {columns} FROM content_items "
                              "WHERE source_type=? AND source_url LIKE ? LIMIT 50",
                              (source_type, f"%{needle}%"))
        else:
            rows = self._rows(f"SELECT {columns} FROM content_items "
                              "WHERE source_type=? AND source_video_id=? LIMIT 5",
                              (source_type, ident))
        for row in rows:
            if key_for(row.get("source_type"), row.get("source_video_id"),
                       row.get("source_url")) == text:
                return row
        return None
