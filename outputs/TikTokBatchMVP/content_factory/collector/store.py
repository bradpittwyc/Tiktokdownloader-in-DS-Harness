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
执行方通过 claim_jobs / mark_done / mark_failed 回写，或者由
ContentCollector.reconcile() 依据内容库的实际结果自动收敛。

两个容易踩的点：
- 「取出待办」必须用 claim_jobs()（CAS），不能用「先查 pending 再标 running」——
  后者在并发下会把同一个任务交给两个调用者，同一条视频就被下载两遍。
- 一轮采集里的多条写入要尽量合并成一次事务（enqueue_many / mark_many）：
  一次 sqlite 提交在慢盘上是几百毫秒级，一条一个事务会让一轮采集变成几分钟。
"""

import threading
from contextlib import closing

from content_factory.factory_store import new_id

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
    def __init__(self, store, now=None):
        self.base = store
        self._now = now                        # 可注入时钟（与 CreatorMonitorService 一致）
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
        """一个事务里执行多条写语句。

        为什么必须批量：一次 sqlite 提交在慢盘上是几百毫秒级，而一轮采集动辄
        几十个任务 —— 一条一个事务会让「首次检查一个 400 条内容的创作者」变成
        十分钟。批量之后一轮采集只有个位数次提交。
        """
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

    def _stamp(self):
        from content_factory.creator_monitor.intervals import stamp
        return stamp(self._now() if self._now else None)

    # ---- job 写入 ------------------------------------------------------
    def _job_row(self, job, stamp):
        """把调用方给的字段补成完整一行（与 SCHEMA 的默认值保持一致）。"""
        row = {key: job.get(key) for key in JOB_FIELDS if job.get(key) is not None}
        row.setdefault("state", "pending")
        row["id"] = str(job.get("id") or new_id("cjob"))
        row["created_at"] = stamp
        row["updated_at"] = stamp
        if not row.get("scheduled_at"):
            row["scheduled_at"] = stamp
        for key in ("batch_id", "creator_id", "creator_handle", "source_url", "content_key",
                    "title", "description", "cover", "upload_date", "content_item_id",
                    "last_error", "finished_at", "source_video_id"):
            row.setdefault(key, "")
        row.setdefault("source_type", "tiktok")
        row.setdefault("kind", "video")
        row.setdefault("priority", "中")
        row["duration"] = _as_int(row.get("duration"))
        row["attempts"] = _as_int(row.get("attempts"))
        row["max_attempts"] = _as_int(row.get("max_attempts")) or 3
        return row

    def enqueue(self, job):
        """建一个任务；同一个 content_key 已有未完成任务时返回既有任务，不新建。"""
        result = self.enqueue_many([job])
        if result["created"]:
            return {"created": True, "duplicate": False, "job": result["created"][0]}
        if result["duplicates"]:
            return {"created": False, "duplicate": True, "job": result["duplicates"][0]["job"]}
        return {"created": False, "duplicate": False, "error": "缺少 content_key", "job": None}

    def enqueue_many(self, jobs):
        """批量建任务：一次提交，且同一个 content_key 只会有一条未完成任务。

        并发保护有两层：先读一次活跃 key 集合（绝大多数重复在这里就被挡掉），
        再由部分唯一索引 + ON CONFLICT DO NOTHING 在数据库层面兜底。
        """
        prepared, skipped = [], []
        for job in jobs or []:
            job = job or {}
            content_key = str(job.get("content_key") or "").strip()
            if not content_key:
                skipped.append(job)
                continue
            prepared.append((content_key, job))
        if not prepared:
            return {"created": [], "duplicates": [], "createdCount": 0,
                    "duplicateCount": len(skipped), "jobs": []}

        active = {row["content_key"]: row for row in
                  self._rows("SELECT * FROM collection_jobs WHERE content_key<>'' "
                             "AND state IN ('pending','running')")}
        stamp = self._stamp()
        created, duplicates, statements, pending_rows = [], [], [], []
        for content_key, job in prepared:
            existing = active.get(content_key)
            if existing:
                duplicates.append({"created": False, "duplicate": True, "job": existing})
                continue
            row = self._job_row(job, stamp)
            columns = list(row)
            statements.append((
                "INSERT INTO collection_jobs (" + ", ".join(columns) + ") VALUES ("
                + ", ".join("?" for _ in columns) + ") "
                "ON CONFLICT(content_key) WHERE state IN ('pending','running') DO NOTHING",
                tuple(row[key] for key in columns)))
            pending_rows.append(row)
            active[content_key] = row                 # 同批次里再来一次也算重复

        if statements:
            self.init()
            with self._lock, closing(self.base.connect()) as connection, connection:
                for (sql, params), row in zip(statements, pending_rows):
                    cursor = connection.execute(sql, params)
                    if cursor.rowcount == 1:
                        created.append(row)
                    else:
                        # 并发下被别人抢先建了同一个任务：把既有任务还回去，仍然算重复
                        duplicates.append({"created": False, "duplicate": True,
                                           "job": self._row("SELECT * FROM collection_jobs "
                                                            "WHERE content_key=? AND state IN "
                                                            "('pending','running')",
                                                            (row["content_key"],))})
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
        """content_key -> 最近一次任务（用于判断「要不要重试」）。

        用 rowid 而不是 id 兜底排序：同一秒内建的任务 created_at 完全相同，
        按 uuid 排序等于随机取一条，重试次数会算错。
        """
        params = []
        sql = "SELECT *, rowid AS _seq FROM collection_jobs"
        if creator_id:
            sql += " WHERE creator_id=?"
            params.append(creator_id)
        sql += f" ORDER BY created_at DESC, _seq DESC LIMIT {int(limit)}"
        latest = {}
        for row in self._rows(sql, tuple(params)):
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
    def _transition_sql(self, job_id, state, fields, stamp, guard=None):
        payload = {key: value for key, value in (fields or {}).items()
                   if key in ("content_item_id", "last_error", "finished_at", "attempts",
                              "max_attempts", "priority", "scheduled_at")}
        payload["state"] = state
        payload["updated_at"] = stamp
        if state in ("done", "failed", "skipped", "cancelled") and "finished_at" not in payload:
            payload["finished_at"] = payload["updated_at"]
        clause = ", ".join(f"{key}=?" for key in payload)
        sql = f"UPDATE collection_jobs SET {clause} WHERE id=?"
        if guard:
            sql += f" AND {guard}"
        return sql, (*payload.values(), str(job_id or ""))

    def _transition(self, job_id, state, **fields):
        sql, params = self._transition_sql(job_id, state, fields, self._stamp())
        return self._write(sql, params)

    def claim_jobs(self, job_ids, limit=None):
        """把待办任务原子地变成 running（CAS），返回真正抢到的 id 列表。

        为什么必须是 CAS：`plan()` 先 SELECT pending、再逐个标 running，
        中间没有任何互斥 —— 两个「开始下载」按钮、或者常驻轮询和手动点击同时发生，
        同一条内容就会被下载两遍（而且第一遍的文件还没落盘，去重也救不了）。
        这里把判断和置位压进同一条 UPDATE，只有 rowcount=1 的那个调用者算抢到。
        """
        ids = [str(job_id) for job_id in (job_ids or []) if job_id]
        if limit:
            ids = ids[:int(limit)]
        if not ids:
            return []
        stamp = self._stamp()
        statements = [(
            "UPDATE collection_jobs SET state='running', attempts=attempts+1, updated_at=? "
            "WHERE id=? AND state='pending'", (stamp, job_id)) for job_id in ids]
        self.init()
        claimed = []
        with self._lock, closing(self.base.connect()) as connection, connection:
            for job_id, (sql, params) in zip(ids, statements):
                if connection.execute(sql, params).rowcount == 1:
                    claimed.append(job_id)
        return claimed

    def mark_many(self, transitions):
        """批量流转：[{"id","state","content_item_id","error"}, ...]，一次提交。"""
        stamp = self._stamp()
        statements = []
        for item in transitions or []:
            fields = {"content_item_id": item.get("content_item_id", ""),
                      "last_error": item.get("error", "") or item.get("last_error", "")}
            sql, params = self._transition_sql(item.get("id"), item.get("state") or "done",
                                               fields, stamp)
            statements.append((sql, params))
        return self.write_all(statements)

    def mark_running(self, job_id, attempts=None):
        """单条置 running；想拿到「是否抢到」请用 claim_jobs()。"""
        fields = {"attempts": _as_int(attempts)} if attempts is not None else {}
        sql, params = self._transition_sql(job_id, "running", fields, self._stamp(),
                                           guard="state IN ('pending','running')")
        return self._write(sql, params)

    def mark_done(self, job_id, content_item_id="", error=""):
        return self._transition(job_id, "done", content_item_id=content_item_id, last_error=error)

    def mark_failed(self, job_id, error=""):
        return self._transition(job_id, "failed", last_error=str(error or "")[:1000])

    def mark_skipped(self, job_id, reason=""):
        return self._transition(job_id, "skipped", last_error=str(reason or "")[:1000])

    def cancel(self, job_id, reason="任务已取消"):
        return self._transition(job_id, "cancelled", last_error=reason)

    def cancel_for_creator(self, creator_id):
        stamp = self._stamp()
        return self._write(
            "UPDATE collection_jobs SET state='cancelled', last_error='创作者已删除', "
            "finished_at=?, updated_at=? WHERE creator_id=? AND state IN ('pending','running')",
            (stamp, stamp, str(creator_id or "")))

    def requeue_stale(self, seconds=3600):
        """把卡在 running 太久的任务退回 pending（进程被杀后能自愈）。"""
        from content_factory.creator_monitor.intervals import shift
        stamp = self._stamp()
        cutoff = shift(stamp, -int(seconds))
        return self._write(
            "UPDATE collection_jobs SET state='pending', last_error='上次执行超时，已重新排队', "
            "updated_at=? WHERE state='running' AND updated_at<?", (stamp, cutoff))

    def failed_jobs(self, limit=50):
        return self.jobs(state="failed", limit=limit)

    # ---- run 记录 ------------------------------------------------------
    def record_run(self, creator_id="", creator_handle="", trigger="poll", state="success",
                   started_at="", finished_at="", discovered=0, fresh=0, queued=0,
                   duplicates=0, filtered=0, existing=0, error="", message="", warning="",
                   needs_verification=False, complete=False, batch_id="", run_id=None):
        run_id = run_id or new_id("crun")
        stamp = self._stamp()
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
