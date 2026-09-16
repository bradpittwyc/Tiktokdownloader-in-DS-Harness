"""任务队列：入队 / 去重 / 认领 / 完成 / 失败重试 / 恢复 / 并发执行 / 事件。

分层：
    jobs.py        Job 模型 + 任务表（持久化，不管策略）
    retry.py       重试策略（纯函数，不管存储）
    queue.py  ←── 本文件：把上面两者编排成「队列」；对外只暴露
                   enqueue / claim / finish / fail / cancel / recover / start / stop
    orchestrator.py 把队列接到真实的阶段实现上（下载 / 转写 / AI 标注）

这一层不认识内容库、不认识界面、不认识模型 API —— 只认识「任务」。
好处是它可以被单测直接怼：注入假 handler，就能把
FIFO、优先级、并发上限、崩溃恢复、事件序列全部跑一遍。

线程模型：
- `WorkerPool` 起 N 个线程，每个线程循环「认领一条 -> 交给 handler -> 写回结果」；
- 认领是数据库原子操作（BEGIN IMMEDIATE），所以**并发上限 = 线程数**，
  不会因为两个线程同时抢而超发；
- 同一条内容的内容级锁在 SQL 里判定，避免一条内容同时跑两个阶段。
"""

import sqlite3
import threading
import time
from collections import deque

from .jobs import (ACTIVE_STATES, STATE_CANCELLED, STATE_DONE, STATE_FAILED,
                   STATE_PENDING, STATE_QUEUED, STATE_RETRYING, STATE_RUNNING, Job, JobStore,
                   current_run_token, default_dedupe_key, new_job_id, now_text)
from .retry import DEFAULT_POLICIES, NonRetryableError, RetryDecision, RetryPolicy

# ---- 事件名（界面 / 日志按这些名字订阅） -------------------------------
EVENT_QUEUED = "jobQueued"
EVENT_STARTED = "jobStarted"
EVENT_PROGRESS = "jobProgress"
EVENT_SUCCEEDED = "jobSucceeded"
EVENT_FAILED = "jobFailed"
EVENT_RETRYING = "jobRetrying"
EVENT_CANCELLED = "jobCancelled"
EVENT_RECOVERED = "queueRecovered"
EVENT_IDLE = "queueIdle"

EVENT_NAMES = (EVENT_QUEUED, EVENT_STARTED, EVENT_PROGRESS, EVENT_SUCCEEDED, EVENT_FAILED,
               EVENT_RETRYING, EVENT_CANCELLED, EVENT_RECOVERED, EVENT_IDLE)


class JobEvents:
    """任务事件总线：一份历史 + 任意个订阅者 + 一个外部 sink（桥接层用它转发到界面）。

    设计取舍：
    - 事件发射**绝不抛异常**：界面/日志出问题不能把任务搞挂（沿用 pipeline._report 的教训）；
    - 保留最近 N 条历史：测试和「刚打开界面时补状态」都要用；
    - sink 与订阅者分开：sink 是应用的（webview 事件队列），订阅者是进程内的。
    """

    def __init__(self, sink=None, limit=500):
        self._lock = threading.RLock()
        self._sink = sink
        self._limit = max(10, int(limit or 500))
        self._listeners = []
        self._history = deque(maxlen=self._limit)

    def set_sink(self, sink):
        with self._lock:
            self._sink = sink

    def subscribe(self, listener):
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)
        return listener

    def unsubscribe(self, listener):
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)
                return True
        return False

    def emit(self, name, payload=None):
        name = str(name or "")
        data = dict(payload or {})
        data.setdefault("event", name)
        data.setdefault("at", time.time())
        with self._lock:
            self._history.append((name, data))
            listeners = list(self._listeners)
            sink = self._sink
        for listener in listeners:
            try:
                listener(name, dict(data))
            except Exception:
                pass
        if sink is not None:
            try:
                sink(name, dict(data))
            except Exception:
                pass
        return data

    def history(self, name=None, limit=None):
        with self._lock:
            rows = list(self._history)
        if name:
            rows = [row for row in rows if row[0] == name]
        if limit:
            rows = rows[-int(limit):]
        return [dict(payload, event=event) for event, payload in rows]

    def last(self, name=None):
        rows = self.history(name=name, limit=1)
        return rows[0] if rows else None

    def clear(self):
        with self._lock:
            self._history.clear()


class JobQueue:
    """持久化任务队列。

    对外语义（稳定契约，改动要同步 docs/pipeline-core.md）：
    - enqueue(...) -> {"ok", "created", "duplicate", "job"}
      同 (kind, content_id) 已有活跃任务时不重复入队，返回已有任务（created=False）。
    - claim(...) -> Job | None          原子认领并置 running（attempts 在这里 +1）
    - finish(job, result) -> Job        置 done
    - fail(job, error, permanent=False) -> {"job", "retry", "delay", "decision"}
      还能重试则置 retrying（带退避时间），否则置 failed。
    - recover() -> {"recovered", "abandoned", "jobs"}  程序重启 / worker 崩溃后的收敛
    """

    def __init__(self, store=None, path=None, run_token=None, retry=None, policies=None,
                 emit=None, events=None, lease_seconds=900.0, clock=None, id_factory=None):
        self.store = store if isinstance(store, JobStore) else JobStore(
            path or (getattr(store, "path", None)))
        self.retry = retry or RetryPolicy()
        self.policies = dict(DEFAULT_POLICIES)
        if policies:
            self.policies.update(policies)
        self.run_token = run_token or current_run_token()
        self.lease_seconds = float(lease_seconds)
        self.events = events or JobEvents(sink=emit)
        self._clock = clock or time.time
        self._id_factory = id_factory or new_job_id
        self._lock = threading.RLock()

    # ---- 小工具 --------------------------------------------------------
    def now(self):
        return float(self._clock())

    def policy_for(self, kind):
        return self.policies.get(kind) or self.retry

    def set_policy(self, kind, policy):
        with self._lock:
            self.policies[str(kind)] = policy
        return policy

    def _event_payload(self, job, message="", **extra):
        """事件载荷：字段名与既有 enrichProgress 尽量对齐（id / state / stage），
        这样桥接层以后要做转换是纯映射，不用猜。

        带上 payload / result：编排层靠 payload["chain"] 决定下一步接谁，
        这样「链路走到哪了」是随任务持久化的，重启后不会断。
        """
        payload = {
            "jobId": job.id,
            "id": job.content_id,
            "contentId": job.content_id,
            "kind": job.kind,
            "stage": job.kind,
            "state": job.state,
            "stateLabel": job.state_label,
            "attempt": job.attempts,
            "maxAttempts": job.max_attempts,
            "retriesLeft": job.retries_left,
            "priority": job.priority,
            "payload": dict(job.payload),
            "result": dict(job.result),
            "message": message,
            "error": job.error,
            "queue": self.counts(),
        }
        payload.update(extra)
        return payload

    # ---- 入队 ----------------------------------------------------------
    def enqueue(self, kind, content_id="", payload=None, priority=0, max_attempts=None,
                dedupe_key=None, delay=0.0, job_id=None, state=None):
        """入队一条任务。重复（同 dedupe_key 已有活跃任务）时返回已有任务。"""
        kind = str(kind or "").strip()
        if not kind:
            return {"ok": False, "created": False, "duplicate": False, "job": None,
                    "error": "kind 不能为空"}
        content_id = str(content_id or "")
        dedupe_key = dedupe_key or default_dedupe_key(kind, content_id)
        policy = self.policy_for(kind)
        delay = max(0.0, float(delay or 0.0))
        now = self.now()

        existing = self.store.find_active(dedupe_key)
        if existing is not None:
            return {"ok": True, "created": False, "duplicate": True, "job": existing.to_dict(),
                    "error": "", "reason": f"{kind} 阶段已有任务在队列中（{existing.state_label}）"}

        stamp = now_text(now)
        job = Job(
            id=job_id or self._id_factory(),
            seq=0,                                    # 取号交给 JobStore.insert（事务内）
            kind=kind,
            content_id=content_id,
            dedupe_key=dedupe_key,
            state=state or (STATE_QUEUED if delay <= 0 else STATE_PENDING),
            priority=int(priority or 0),
            attempts=0,
            max_attempts=int(max_attempts or policy.max_attempts),
            payload=dict(payload or {}),
            available_at=now + delay,
            created_at=stamp,
            updated_at=stamp,
        )
        try:
            saved = self.store.insert(job)
        except sqlite3.IntegrityError:
            # 数据库部分唯一索引兜底：并发入队时会有一个人撞上
            existing = self.store.find_active(dedupe_key)
            if existing is not None:
                return {"ok": True, "created": False, "duplicate": True, "job": existing.to_dict(),
                        "error": "", "reason": f"{kind} 阶段已有任务在队列中"}
            raise
        self.events.emit(EVENT_QUEUED, self._event_payload(
            saved, f"{kind} 任务已入队（第 {saved.seq} 位）"))
        return {"ok": True, "created": True, "duplicate": False, "job": saved.to_dict(),
                "error": "", "reason": ""}

    def enqueue_many(self, requests, priority=0):
        """批量入队。requests 里每项是 dict（kind / contentId / payload / priority ...）。

        单条非法不影响其它条 —— 批量任务必须「能排进去的先排进去」。
        """
        created, duplicates, rejected = [], [], []
        for request in requests or []:
            if isinstance(request, Job):
                request = {"kind": request.kind, "contentId": request.content_id,
                           "payload": request.payload, "priority": request.priority}
            if not isinstance(request, dict):
                rejected.append({"request": repr(request), "error": "入队项必须是 dict"})
                continue
            kind = request.get("kind") or request.get("stage") or ""
            content_id = request.get("contentId", request.get("content_id", request.get("id", "")))
            result = self.enqueue(
                kind, content_id=content_id,
                payload=request.get("payload"),
                priority=int(request.get("priority", priority) or 0),
                max_attempts=request.get("maxAttempts", request.get("max_attempts")),
                dedupe_key=request.get("dedupeKey", request.get("dedupe_key")),
                delay=request.get("delay", 0.0),
            )
            if not result.get("ok"):
                rejected.append(result)
            elif result["created"]:
                created.append(result["job"])
            else:
                duplicates.append(result["job"])
        return {"ok": True, "created": len(created), "duplicates": len(duplicates),
                "rejected": len(rejected), "jobs": created, "duplicateJobs": duplicates,
                "rejectedJobs": rejected}

    # ---- 查询 ----------------------------------------------------------
    def get(self, job_id):
        return self.store.get(job_id)

    def job_dict(self, job_id):
        job = self.store.get(job_id)
        return job.to_dict() if job else None

    def jobs(self, state=None, states=None, kind=None, content_id=None, limit=200, order="seq ASC"):
        return self.store.jobs(state=state, states=states, kind=kind, content_id=content_id,
                               limit=limit, order=order)

    def active_for_content(self, content_id, kind=None):
        return self.store.find_active_for_content(content_id, kind=kind)

    def counts(self):
        return self.store.counts()

    def state_counts(self):
        counts = self.counts()
        return {state: counts.get(state, 0) for state in
                (STATE_PENDING, STATE_QUEUED, STATE_RUNNING, STATE_RETRYING,
                 STATE_DONE, STATE_FAILED, STATE_CANCELLED)}

    def pending(self, limit=200):
        return self.store.jobs(states=ACTIVE_STATES, limit=limit)

    def is_idle(self):
        counts = self.counts()
        return counts.get("active", 0) == 0

    def has_ready_job(self, kinds=None):
        """现在马上就能被认领的任务存在吗（只读查询，不改任何状态）。"""
        return self.store.next_claimable(kinds=kinds, now=self.now()) is not None

    def wait_for_idle(self, timeout=30.0, poll=0.02):
        deadline = None if timeout is None else self.now() + float(timeout)
        while True:
            if self.is_idle():
                return True
            if deadline is not None and self.now() >= deadline:
                return False
            time.sleep(poll)

    def status(self):
        counts = self.counts()
        return {
            "ok": True,
            "runToken": self.run_token,
            "counts": counts,
            "states": self.state_counts(),
            "leaseSeconds": self.lease_seconds,
            "policies": {kind: policy.to_dict() for kind, policy in self.policies.items()},
            "activeContentIds": sorted(self.store.active_content_ids()),
        }

    # ---- 认领 / 执行 ---------------------------------------------------
    def claim(self, worker_id="", kinds=None, now=None, lease_seconds=None,
              respect_content_lock=True):
        """认领一条可执行任务。attempts 在这里 +1（崩溃也算一次尝试，避免死循环）。

        为什么在认领时而不是完成时计数：进程被 kill 掉时没有机会写"这次失败了"，
        如果完成时才计数，一条能把程序搞崩的任务会被无限重启。
        """
        now = float(now if now is not None else self.now())
        job = self.store.claim(worker_id=worker_id, run_token=self.run_token, kinds=kinds,
                               lease_seconds=self.lease_seconds if lease_seconds is None
                               else lease_seconds,
                               now=now, respect_content_lock=respect_content_lock)
        if job is None:
            return None
        self.events.emit(EVENT_STARTED, self._event_payload(
            job, f"开始执行（第 {job.attempts}/{job.max_attempts} 次尝试）",
            workerId=worker_id or ""))
        return job

    def heartbeat(self, job, seconds=None):
        """续租。长任务（AI 标注可能几分钟）应该定期调用，避免被判成卡死。"""
        job_id = job.id if isinstance(job, Job) else str(job)
        return self.store.heartbeat(job_id, lease_seconds=self.lease_seconds if seconds is None
                                    else seconds, now=self.now())

    def finish(self, job, result=None, message=""):
        """任务成功。已被取消 / 已被别人接管的任务不再改写状态。"""
        job_id = job.id if isinstance(job, Job) else str(job)
        fresh = self.store.get(job_id)
        if fresh is None or fresh.state != STATE_RUNNING:
            return fresh
        now = self.now()
        updated = self.store.update(job_id, state=STATE_DONE, result=dict(result or {}),
                                    error="", finished_at=now_text(now), lease_owner="",
                                    lease_expires_at=0.0)
        self.events.emit(EVENT_SUCCEEDED, self._event_payload(
            updated, message or f"{updated.kind} 完成", result=dict(updated.result)))
        self.events.emit(EVENT_PROGRESS, self._event_payload(updated, message or "已完成"))
        self._emit_idle_if_done()
        return updated

    def fail(self, job, error="", permanent=False, now=None, message=""):
        """任务失败。还能重试 -> retrying（带退避），否则 -> failed。

        返回 {"job", "retry", "delay", "decision"}：调用方（编排层）据此同步内容状态。
        """
        job_id = job.id if isinstance(job, Job) else str(job)
        fresh = self.store.get(job_id)
        if fresh is None:
            return {"ok": False, "error": "任务不存在", "retry": False, "delay": 0.0,
                    "decision": None, "job": None}
        if fresh.state != STATE_RUNNING:
            # 已经被取消或被恢复流程改写：不要覆盖新的状态
            return {"ok": True, "retry": False, "delay": 0.0, "decision": None, "job": fresh,
                    "error": error, "ignored": True}
        now = float(now if now is not None else self.now())
        error = str(error or "任务失败")[:1000]
        policy = self.policy_for(fresh.kind).with_(max_attempts=fresh.max_attempts)
        if permanent:
            decision = RetryDecision(False, 0.0, "永久失败：重试不会改变结果",
                                     fresh.attempts, fresh.max_attempts)
        else:
            decision = policy.decide(fresh.attempts)

        if decision.retry:
            updated = self.store.update(
                job_id, state=STATE_RETRYING, error=error, available_at=now + decision.delay,
                lease_owner="", lease_expires_at=0.0, finished_at="")
            self.events.emit(EVENT_RETRYING, self._event_payload(
                updated, message or f"第 {updated.attempts} 次尝试失败，{decision.delay:.2f}s 后重试",
                delay=decision.delay, nextAttempt=updated.attempts + 1))
            self.events.emit(EVENT_PROGRESS, self._event_payload(updated, message or "等待重试"))
        else:
            updated = self.store.update(
                job_id, state=STATE_FAILED, error=error, lease_owner="", lease_expires_at=0.0,
                finished_at=now_text(now))
            self.events.emit(EVENT_FAILED, self._event_payload(
                updated, message or f"任务失败，已放弃（共尝试 {updated.attempts} 次）",
                reason=decision.reason))
            self.events.emit(EVENT_PROGRESS, self._event_payload(updated, message or "失败"))
            self._emit_idle_if_done()
        return {"ok": True, "retry": decision.retry, "delay": decision.delay,
                "decision": decision.to_dict(), "job": updated, "error": error}

    def cancel(self, job_or_id, reason="已取消"):
        """取消一条活跃任务。正在执行的那次不会被中断（handler 自己决定要不要看取消标记），
        但它的结果会被丢弃（finish/fail 只接受 running 状态）。"""
        job_id = job_or_id.id if isinstance(job_or_id, Job) else str(job_or_id)
        fresh = self.store.get(job_id)
        if fresh is None or fresh.is_terminal:
            return fresh
        now = self.now()
        updated = self.store.update(job_id, state=STATE_CANCELLED, error=str(reason or ""),
                                    finished_at=now_text(now), lease_owner="", lease_expires_at=0.0)
        self.events.emit(EVENT_CANCELLED, self._event_payload(updated, str(reason or "已取消")))
        self._emit_idle_if_done()
        return updated

    def requeue(self, job_or_id, delay=0.0, error="", state=None):
        """把任务放回队列（恢复流程 / 手动重排用）。"""
        job_id = job_or_id.id if isinstance(job_or_id, Job) else str(job_or_id)
        fresh = self.store.get(job_id)
        if fresh is None:
            return None
        delay = max(0.0, float(delay or 0.0))
        target = state or (STATE_QUEUED if delay <= 0 else STATE_RETRYING)
        updated = self.store.update(job_id, state=target, error=str(error or ""),
                                    available_at=self.now() + delay, lease_owner="",
                                    lease_expires_at=0.0, finished_at="")
        self.events.emit(EVENT_QUEUED, self._event_payload(updated, "任务已重新排队"))
        return updated

    def _emit_idle_if_done(self):
        if self.is_idle():
            self.events.emit(EVENT_IDLE, {"id": "", "contentId": "", "state": "idle",
                                          "message": "队列已处理完", "queue": self.counts()})

    # ---- 恢复 ----------------------------------------------------------
    def recover(self, now=None, include_stale=True):
        """程序重启 / worker 崩溃后的收敛。

        对每条「没人管」的 running 任务：
        - 还有重试额度 -> retrying（带退避）或 queued；
        - 额度用尽     -> failed（写明是异常退出，不是普通失败）。
        返回 {"recovered", "abandoned", "jobs"}，编排层据此把内容状态从 running 收回来。
        """
        now = float(now if now is not None else self.now())
        stale = self.store.stale_running(now=now, run_token=self.run_token) if include_stale else []
        recovered, abandoned = [], []
        for job in stale:
            policy = self.policy_for(job.kind).with_(max_attempts=job.max_attempts)
            if policy.allows(job.attempts):
                delay = policy.delay_for(job.attempts)
                target = STATE_RETRYING if delay > 0 else STATE_QUEUED
                updated = self.store.update(
                    job.id, state=target, available_at=now + delay,
                    error=f"执行进程异常退出（已尝试 {job.attempts} 次），已重新排队",
                    lease_owner="", lease_expires_at=0.0, finished_at="")
                recovered.append(updated)
                self.events.emit(EVENT_RETRYING if delay > 0 else EVENT_QUEUED,
                                 self._event_payload(updated, "检测到中断的任务，已重新排队",
                                                     recovered=True, delay=delay))
            else:
                updated = self.store.update(
                    job.id, state=STATE_FAILED, finished_at=now_text(now), lease_owner="",
                    lease_expires_at=0.0,
                    error=f"执行进程异常退出，且已达最大尝试次数（{job.max_attempts}）")
                abandoned.append(updated)
                self.events.emit(EVENT_FAILED, self._event_payload(
                    updated, "中断的任务已放弃（重试额度用尽）", recovered=True))
        summary = {
            "ok": True,
            "recovered": len(recovered),
            "abandoned": len(abandoned),
            "jobs": [job.to_dict() for job in recovered],
            "abandonedJobs": [job.to_dict() for job in abandoned],
            "checkedAt": now_text(now),
        }
        if stale:
            self.events.emit(EVENT_RECOVERED, dict(summary, message=(
                f"恢复 {len(recovered)} 条中断任务，放弃 {len(abandoned)} 条")))
        return summary

    def reap_stale(self, now=None):
        """租约过期的任务回收（在跑的 worker 卡死 / 被杀）。"""
        return self.recover(now=now, include_stale=True)

    def clear(self, terminal_only=True):
        return self.store.clear(terminal_only=terminal_only)

    def close(self):
        """关闭底层共享连接（测试清理 / 程序退出）。"""
        return self.store.close()


class _Heartbeat:
    """任务执行期间的续租器。

    为什么需要：租约（lease）是「这个任务有人在管」的凭证，卡死的 worker 会被
    回收重排。但真实阶段可能跑很久（ASR 几分钟、AI 调用几十秒），如果不管它，
    一个还在正常干活的任务会被误判成孤儿 —— 那就变成同一条内容被两个 worker
    同时跑了，正是需求里明确要避免的事。所以执行期间定时续租。
    """

    def __init__(self, queue, job, interval):
        self._queue = queue
        self._job = job
        self._interval = float(interval or 0.0)
        self._timer = None
        self._stopped = False

    def start(self):
        if self._interval <= 0:
            return self
        self._arm()
        return self

    def _arm(self):
        if self._stopped:
            return
        self._timer = threading.Timer(self._interval, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self):
        if self._stopped:
            return
        try:
            self._queue.heartbeat(self._job, seconds=self._interval * 3)
        except Exception:
            pass
        self._arm()

    def stop(self):
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        return True


class WorkerPool:
    """固定并发度的执行池。

    - 并发上限 = 线程数（认领是原子的，所以不会超发）；
    - handler 抛异常 -> 任务失败并进入重试/失败判定，线程继续活着；
    - handler 返回 {"ok": False, "error": ...} 也算失败（约定俗成，方便适配器写）；
      带 "permanent": True 或 "retryable": False 时不再重试；
    - 执行期间自动续租，长任务不会被自己的租约误伤。
    """

    def __init__(self, queue, handler, concurrency=2, poll_interval=0.05, name="ContentJobs",
                 on_error=None, clock=None, reap_interval=5.0, heartbeat=True):
        self.queue = queue
        self.handler = handler
        self.concurrency = max(1, int(concurrency or 1))
        self.poll_interval = max(0.005, float(poll_interval or 0.05))
        self.name = name or "ContentJobs"
        self.on_error = on_error
        self.reap_interval = max(0.0, float(reap_interval or 0.0))
        self.heartbeat_enabled = bool(heartbeat)
        self._clock = clock or queue.now
        self._stop = threading.Event()
        self._threads = []
        self._lock = threading.RLock()
        self._active = 0
        self._max_active = 0
        self._processed = 0
        self._succeeded = 0
        self._failed = 0
        self._last_reap = 0.0
        self._started = False

    # ---- 生命周期 ------------------------------------------------------
    def start(self):
        with self._lock:
            if self._started:
                return self
            self._stop.clear()
            self._threads = [threading.Thread(target=self._loop, args=(index,),
                                              name=f"{self.name}-{index}", daemon=True)
                             for index in range(self.concurrency)]
            for thread in self._threads:
                thread.start()
            self._started = True
            return self

    def stop(self, wait=True, timeout=5.0):
        with self._lock:
            self._stop.set()
            threads = list(self._threads)
            self._threads = []
            self._started = False
        if wait:
            for thread in threads:
                thread.join(timeout=timeout)
        return True

    def join(self, timeout=None):
        for thread in list(self._threads):
            thread.join(timeout=timeout)
        return True

    @property
    def started(self):
        return self._started

    @property
    def active(self):
        with self._lock:
            return self._active

    @property
    def alive(self):
        return any(thread.is_alive() for thread in list(self._threads))

    def stats(self):
        with self._lock:
            return {
                "started": self._started,
                "concurrency": self.concurrency,
                "active": self._active,
                "maxActive": self._max_active,
                "processed": self._processed,
                "succeeded": self._succeeded,
                "failed": self._failed,
                "threads": [thread.name for thread in self._threads],
            }

    # ---- 主循环 --------------------------------------------------------
    def _loop(self, index):
        worker_id = f"{self.name}-{index}"
        while not self._stop.is_set():
            job = self.queue.claim(worker_id=worker_id)
            if job is None:
                self._maybe_reap()
                self._stop.wait(self.poll_interval)
                continue
            self._run_one(job)

    def _maybe_reap(self):
        if not self.reap_interval:
            return
        now = float(self._clock())
        if now - self._last_reap < self.reap_interval:
            return
        self._last_reap = now
        try:
            self.queue.reap_stale(now=now)
        except Exception:
            pass

    def _run_one(self, job):
        with self._lock:
            self._active += 1
            self._max_active = max(self._max_active, self._active)
        beat = _Heartbeat(self.queue, job, self._heartbeat_interval()).start()
        try:
            self._execute(job)
        finally:
            beat.stop()
            with self._lock:
                self._active -= 1
                self._processed += 1

    def _heartbeat_interval(self):
        """续租间隔：租约的三分之一（下限 50ms，防止测试里的小租约不生效）。"""
        if not self.heartbeat_enabled:
            return 0.0
        lease = float(getattr(self.queue, "lease_seconds", 0.0) or 0.0)
        if lease <= 0:
            return 0.0
        return max(0.05, lease / 3.0)

    def _execute(self, job):
        """执行一条已认领的任务并写回结果。异常绝不外泄（否则线程会死）。"""
        try:
            outcome = self.handler(job)
        except Exception as exc:                    # noqa: BLE001 —— 任务失败不能杀线程
            with self._lock:
                self._failed += 1
            permanent = isinstance(exc, NonRetryableError)
            self.queue.fail(job, error=f"{type(exc).__name__}: {exc}", permanent=permanent)
            if self.on_error:
                try:
                    self.on_error(job, exc)
                except Exception:
                    pass
            return False
        if not isinstance(outcome, dict):
            outcome = {"ok": True, "value": outcome}
        if outcome.get("ok", True):
            with self._lock:
                self._succeeded += 1
            self.queue.finish(job, result=outcome, message=str(outcome.get("message") or ""))
            return True
        with self._lock:
            self._failed += 1
        permanent = bool(outcome.get("permanent")) or outcome.get("retryable") is False
        self.queue.fail(job, error=str(outcome.get("error") or "任务失败"), permanent=permanent)
        return False

    def run_once(self, worker_id=None):
        """在当前线程里执行一条任务（测试 / 手动步进用）。返回 Job | None。"""
        job = self.queue.claim(worker_id=worker_id or f"{self.name}-manual")
        if job is None:
            return None
        self._run_one(job)
        return job

    def run_until_idle(self, timeout=30.0, poll=0.02):
        """等到「队列里没有活跃任务 + 没有线程在跑」。返回是否真的等到了。"""
        deadline = None if timeout is None else float(self._clock()) + float(timeout)
        while True:
            if self.queue.is_idle() and self.active == 0:
                return True
            if deadline is not None and float(self._clock()) >= deadline:
                return False
            time.sleep(poll)
